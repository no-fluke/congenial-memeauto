"""
╔══════════════════════════════════════════════════════════════╗
║        AUTOMATED SUBJECT-WISE SCHEDULER  v3.0               ║
║                                                              ║
║  COMMANDS:                                                   ║
║   /addschedule  — Wizard to create a new auto-batch         ║
║   /schedules   — List all your active batches                ║
║   /delschedule <id> — Delete a batch                         ║
║   /runnow <id> — Run a batch right now (for testing)         ║
║                                                              ║
║  FLOW:                                                       ║
║   1. /addschedule → login → pick course → bot lists all     ║
║      subjects → user types  1,2,3  to pick which ones       ║
║      get auto-updated (ALL topics inside those subjects).    ║
║   2. Set time + timezone + repeat + supergroup.              ║
║   3. At the scheduled time the bot:                          ║
║      • Fetches ALL topics for each selected subject.         ║
║      • Creates a forum thread per topic (cached in Mongo).  ║
║      • Checks which lecture IDs are already downloaded       ║
║        (stored in Mongo) → skips them.                       ║
║      • Downloads only NEW lectures into the right thread.   ║
║      • Optionally downloads new test series into its own    ║
║        "📝 Test Series" thread.                              ║
║   4. User can set up 4-6 batches (different courses /        ║
║      subjects / times) — each is independent in Mongo.       ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import os
import tempfile
from datetime import datetime

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

DAY_MAP = {
    "mon": "0", "tue": "1", "wed": "2", "thu": "3",
    "fri": "4", "sat": "5", "sun": "6",
    "monday": "0", "tuesday": "1", "wednesday": "2",
    "thursday": "3", "friday": "4", "saturday": "5", "sunday": "6",
}


# ══════════════════════════════════════════════════════════════════════════════
#  FORUM TOPIC HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def get_or_create_forum_topic(bot_client, supergroup_id: int,
                                     topic_name: str, db) -> int | None:
    """
    Returns the Telegram message_thread_id for a named forum topic.
    Creates it if it doesn't exist yet, caches result in MongoDB.
    Returns None if the group has no forum mode / bot lacks perms.
    """
    cached = db.get_forum_topic(supergroup_id, topic_name)
    if cached:
        return cached
    try:
        result = await bot_client.create_forum_topic(supergroup_id, topic_name)
        tid = result.id
        db.save_forum_topic(supergroup_id, topic_name, tid)
        logger.info(f"[Forum] created '{topic_name}' → thread {tid}")
        return tid
    except Exception as e:
        logger.warning(f"[Forum] could not create '{topic_name}': {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  JOB EXECUTOR  — called by APScheduler at the scheduled time
# ══════════════════════════════════════════════════════════════════════════════

async def run_scheduled_job(schedule_id: str):
    """
    Main job.  For every selected subject it fetches all topics,
    creates forum threads, skips already-downloaded lectures,
    downloads only new ones.  Then handles test series if enabled.
    """
    from db import db as _db
    import aiohttp
    import main as _main

    doc = _db.get_schedule_by_id(schedule_id)
    if not doc:
        logger.warning(f"[Scheduler] schedule {schedule_id} missing, skipping")
        return

    user_id    = doc["user_id"]
    supergroup = doc["supergroup_id"]
    api_base   = doc["api_base"]
    token      = doc["token"]
    appx_uid   = doc["appx_user_id"]
    course_id  = doc["course_id"]
    cname      = doc.get("course_name", f"course_{course_id}")
    subjects   = doc.get("subjects", [])   # [{subjectid, subject_name}]
    inc_series = doc.get("include_test_series", False)
    dl_type    = doc.get("dl_type", "3")
    quality    = doc.get("quality", "480p")
    brand      = doc.get("brand", "AutoBot")
    watermark  = doc.get("watermark") or None

    want_video = dl_type in ("1", "3", "6")
    want_pdf   = dl_type in ("2", "3", "6")
    want_quiz  = dl_type in ("3", "4", "6")

    bot = _main.bot
    hdr = _main.make_headers(token, appx_uid)

    try:
        await bot.send_message(
            supergroup,
            f"⏰ **Auto-schedule fired**\n"
            f"📚 `{cname}`\n"
            f"📂 Subjects: {len(subjects)}  |  "
            f"🕐 `{datetime.utcnow().strftime('%d %b %Y %H:%M UTC')}`"
        )
    except Exception as e:
        logger.error(f"[Scheduler] start-notify failed: {e}")
        return

    downloads_dir = os.path.join(tempfile.gettempdir(), "downloads")
    os.makedirs(downloads_dir, exist_ok=True)
    _main.cancel_requested[user_id] = False

    total_success = total_failed = 0

    async with aiohttp.ClientSession() as session:

        # ════════════════════════════════════════════════════════════════
        #  FOR EACH SELECTED SUBJECT → fetch all its topics → new lecs
        # ════════════════════════════════════════════════════════════════
        for subj in subjects:
            if _main.cancel_requested.get(user_id, False):
                break

            sid = subj["subjectid"]
            sn  = subj["subject_name"]

            # Fetch ALL topics under this subject (auto-discovers new ones)
            all_topics = await _main.get_topics(
                session, api_base, course_id, sid, hdr
            )
            if not all_topics:
                await bot.send_message(
                    supergroup, f"⚠️ `{sn}` — no topics found."
                )
                continue

            for topic in all_topics:
                if _main.cancel_requested.get(user_id, False):
                    break

                tid = topic.get("topicid")
                tn  = topic.get("topic_name", f"topic_{tid}")

                # Get / create the forum thread for this topic
                thread_id = await get_or_create_forum_topic(
                    bot, supergroup, f"📌 {sn} — {tn}", _db
                )

                # Fetch all lectures in this topic
                all_lecs = await _main.get_lectures(
                    session, api_base, course_id, sid, tid, hdr
                )
                if not all_lecs:
                    continue

                # Keep only lectures not yet downloaded for this schedule
                new_lecs = []
                for lec in all_lecs:
                    lid = str(lec.get("id", ""))
                    if lid and not _db.is_lecture_downloaded(schedule_id, lid):
                        lec["_subject"] = sn
                        lec["_topic"]   = tn
                        new_lecs.append(lec)

                if not new_lecs:
                    # Silently skip — no noise for topics with nothing new
                    continue

                await bot.send_message(
                    supergroup,
                    f"📥 **{sn} → {tn}**\n"
                    f"{len(new_lecs)} new lecture(s) found, downloading…",
                    message_thread_id=thread_id
                )

                # Build a minimal Message proxy for run_lecture_loop
                _thread_id_capture = thread_id  # capture for closure

                class _Msg:
                    class _U:
                        id = user_id
                    from_user = _U()
                    chat = type("C", (), {"id": supergroup})()

                    async def reply_text(self, text, **kw):
                        try:
                            await bot.send_message(
                                supergroup, text,
                                message_thread_id=_thread_id_capture, **kw
                            )
                        except Exception:
                            pass

                s, f = await _run_loop_with_thread(
                    bot_client      = bot,
                    m               = _Msg(),
                    session         = session,
                    api_base        = api_base,
                    cid             = course_id,
                    cname           = cname,
                    hdr             = hdr,
                    appx_user_id    = appx_uid,
                    channel_id      = supergroup,
                    thread_id       = thread_id,
                    want_video      = want_video,
                    want_pdf        = want_pdf,
                    want_quiz       = want_quiz,
                    prefer_quality  = quality,
                    batch_watermark = watermark,
                    session_credit  = brand,
                    lectures        = new_lecs,
                    downloads_dir   = downloads_dir,
                    schedule_id     = schedule_id,
                    db              = _db,
                )
                total_success += s
                total_failed  += f

                await asyncio.sleep(0.5)

        # ════════════════════════════════════════════════════════════════
        #  TEST SERIES
        # ════════════════════════════════════════════════════════════════
        if inc_series and not _main.cancel_requested.get(user_id, False):
            series_thread = await get_or_create_forum_topic(
                bot, supergroup, "📝 Test Series", _db
            )

            class _MsgSeries:
                class _U:
                    id = user_id
                from_user = _U()
                chat = type("C", (), {"id": supergroup})()

                async def reply_text(self, text, **kw):
                    try:
                        await bot.send_message(
                            supergroup, text,
                            message_thread_id=series_thread, **kw
                        )
                    except Exception:
                        pass

            # Patch send methods for test series thread too
            _orig_sm = bot.send_message
            _orig_sd = bot.send_document

            async def _sm_ts(chat_id, text, **kw):
                if chat_id == supergroup and "message_thread_id" not in kw:
                    kw["message_thread_id"] = series_thread
                return await _orig_sm(chat_id, text, **kw)

            async def _sd_ts(chat_id, **kw):
                if chat_id == supergroup and "message_thread_id" not in kw:
                    kw["message_thread_id"] = series_thread
                return await _orig_sd(chat_id, **kw)

            bot.send_message  = _sm_ts
            bot.send_document = _sd_ts
            try:
                await _main.download_test_series(
                    bot_client     = bot,
                    m              = _MsgSeries(),
                    session        = session,
                    api_base       = api_base,
                    user_id        = appx_uid,
                    cid            = course_id,
                    hdr            = hdr,
                    course_dir     = downloads_dir,
                    channel_id     = supergroup,
                    cname          = cname,
                    auto_all       = True,
                    session_credit = brand,
                )
            finally:
                bot.send_message  = _orig_sm
                bot.send_document = _orig_sd

    await bot.send_message(
        supergroup,
        f"✅ **Schedule complete!**\n"
        f"📦 `{cname}` — ✓ {total_success} done  ✗ {total_failed} failed"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  THREAD-AWARE WRAPPER FOR run_lecture_loop
#  Patches bot send methods to auto-inject message_thread_id, then restores.
# ══════════════════════════════════════════════════════════════════════════════

async def _run_loop_with_thread(
    bot_client, m, session, api_base, cid, cname, hdr, appx_user_id,
    channel_id, thread_id, want_video, want_pdf, want_quiz,
    prefer_quality, batch_watermark, session_credit,
    lectures, downloads_dir, schedule_id, db,
):
    import main as _main
    import saini as helper

    # Patch bot send methods
    _orig_sm = bot_client.send_message
    _orig_sd = bot_client.send_document
    _orig_sv = bot_client.send_video

    async def _sm(chat_id, text, **kw):
        if chat_id == channel_id and thread_id and "message_thread_id" not in kw:
            kw["message_thread_id"] = thread_id
        return await _orig_sm(chat_id, text, **kw)

    async def _sd(chat_id, **kw):
        if chat_id == channel_id and thread_id and "message_thread_id" not in kw:
            kw["message_thread_id"] = thread_id
        return await _orig_sd(chat_id, **kw)

    async def _sv(chat_id, **kw):
        if chat_id == channel_id and thread_id and "message_thread_id" not in kw:
            kw["message_thread_id"] = thread_id
        return await _orig_sv(chat_id, **kw)

    bot_client.send_message  = _sm
    bot_client.send_document = _sd
    bot_client.send_video    = _sv

    # Patch helper.send_vid to pass topic_id (saini.py already supports it)
    _orig_hv = helper.send_vid
    async def _hv(b, msg, cc, filename, thumb, name, prog,
                  ch_id, topic_id=None, watermark_text=None):
        return await _orig_hv(
            b, msg, cc, filename, thumb, name, prog, ch_id,
            topic_id=thread_id or topic_id,
            watermark_text=watermark_text,
        )
    helper.send_vid = _hv

    try:
        success, failed = await _main.run_lecture_loop(
            bot_client      = bot_client,
            m               = m,
            session         = session,
            api_base        = api_base,
            cid             = cid,
            cname           = cname,
            hdr             = hdr,
            appx_user_id    = appx_user_id,
            channel_id      = channel_id,
            want_video      = want_video,
            want_pdf        = want_pdf,
            want_quiz       = want_quiz,
            prefer_quality  = prefer_quality,
            batch_watermark = batch_watermark,
            session_credit  = session_credit,
            lectures        = lectures,
            start_idx       = 0,
            downloads_dir   = downloads_dir,
        )
    finally:
        bot_client.send_message  = _orig_sm
        bot_client.send_document = _orig_sd
        bot_client.send_video    = _orig_sv
        helper.send_vid          = _orig_hv

    # Mark every lecture as downloaded so next run skips it
    for lec in lectures:
        lid = str(lec.get("id", ""))
        if lid:
            db.mark_lecture_downloaded(schedule_id, lid)

    return success, failed


# ══════════════════════════════════════════════════════════════════════════════
#  APScheduler helpers
# ══════════════════════════════════════════════════════════════════════════════

def _build_trigger(doc: dict):
    repeat   = doc.get("repeat", "daily")
    time_str = doc.get("time", "09:00")
    tz_name  = doc.get("timezone", "Asia/Kolkata")
    try:
        hour, minute = [int(x) for x in time_str.split(":")]
    except Exception:
        hour, minute = 9, 0
    user_tz = pytz.timezone(tz_name)
    if repeat == "daily":
        return CronTrigger(hour=hour, minute=minute, timezone=user_tz)
    elif repeat == "once":
        run_date = doc.get("run_date")
        if run_date:
            dt = datetime.strptime(run_date, "%Y-%m-%d")
        else:
            dt = datetime.now(user_tz)
        return CronTrigger(
            year=dt.year, month=dt.month, day=dt.day,
            hour=hour, minute=minute, timezone=user_tz
        )
    else:
        days = ",".join(
            DAY_MAP[d.strip().lower()]
            for d in repeat.split(",")
            if d.strip().lower() in DAY_MAP
        )
        return CronTrigger(
            day_of_week=days or "*", hour=hour, minute=minute, timezone=user_tz
        )


def register_schedule(doc: dict):
    sid    = str(doc["_id"])
    job_id = f"sched_{sid}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    trigger = _build_trigger(doc)
    scheduler.add_job(
        run_scheduled_job, trigger=trigger, id=job_id,
        args=[sid], replace_existing=True, misfire_grace_time=3600,
    )
    logger.info(
        f"[Scheduler] registered {job_id} — "
        f"{doc.get('repeat')} @ {doc.get('time')} {doc.get('timezone')}"
    )


def load_all_schedules():
    from db import db as _db
    try:
        docs = _db.get_all_schedules()
        for doc in docs:
            if doc.get("active", True):
                register_schedule(doc)
        logger.info(f"[Scheduler] {len(docs)} schedule(s) loaded from DB")
    except Exception as e:
        logger.error(f"[Scheduler] load error: {e}")


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        load_all_schedules()
        logger.info("[Scheduler] APScheduler started ✅")


# ══════════════════════════════════════════════════════════════════════════════
#  /addschedule WIZARD
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_addschedule(bot_client, m):
    import aiohttp
    import main as _main
    from db import db as _db
    from vars import CREDIT

    uid = m.from_user.id
    cid = m.chat.id

    async def ask(prompt, timeout=120):
        msg = await m.reply_text(prompt)
        try:
            rep  = await bot_client.listen(cid, timeout=timeout)
            text = (rep.text or "").strip()
            await rep.delete()
            await msg.delete()
            if text.lower() == "/cancel":
                await m.reply_text("❌ Cancelled.")
                return None
            return text
        except asyncio.TimeoutError:
            await msg.edit_text("⏰ Timed out. Use /addschedule to restart.")
            return None

    await m.reply_text(
        "📅 **Add Automated Batch Schedule**\n"
        "You can create up to 6 independent batches.\n"
        "_(Send /cancel at any step to quit)_"
    )

    # ── Step 1: API domain ───────────────────────────────────────────────────
    domain = await ask(
        "**[1/8] API Domain**\n"
        "Send without `https://`\n"
        "e.g. `parmaracademyapi.teachx.in`"
    )
    if not domain: return
    api_base = f"https://{domain}" if not domain.startswith("http") else domain

    # ── Step 2: Login ────────────────────────────────────────────────────────
    await m.reply_text("**[2/8] Login**")
    token, appx_uid, uname = await _main.appx_login(bot_client, m, api_base)
    if not token: return
    hdr = _main.make_headers(token, appx_uid)
    await m.reply_text(f"✅ Logged in as `{uname}`")

    # ── Step 3: Course ───────────────────────────────────────────────────────
    status = await m.reply_text("⏳ Fetching your courses…")
    async with aiohttp.ClientSession() as session:
        courses = await _main.get_courses(session, api_base, appx_uid, hdr)
    await status.delete()
    if not courses:
        await m.reply_text("❌ No courses found."); return

    course_lines = "\n".join(
        f"`{i+1}` — {c.get('course_name', '?')}  _(id: {c.get('id', '?')})_"
        for i, c in enumerate(courses)
    )
    pick = await ask(
        f"**[3/8] Pick Course**\n\n{course_lines}\n\nSend a number:",
        timeout=60
    )
    if not pick: return
    try:
        chosen_course = courses[int(pick) - 1]
    except (ValueError, IndexError):
        await m.reply_text("❌ Invalid choice."); return

    course_id = str(chosen_course.get("id", ""))
    cname     = chosen_course.get("course_name", f"course_{course_id}")

    # ── Step 4: Pick subjects ────────────────────────────────────────────────
    status = await m.reply_text("⏳ Fetching subjects…")
    async with aiohttp.ClientSession() as session:
        all_subjects = await _main.get_subjects(session, api_base, course_id, hdr)
    await status.delete()
    if not all_subjects:
        await m.reply_text("❌ No subjects found."); return

    subj_lines = "\n".join(
        f"`{i+1}` — {s.get('subject_name', '?')}"
        for i, s in enumerate(all_subjects)
    )
    sp = await ask(
        f"**[4/8] Select Subjects to Auto-Update**\n\n"
        f"{subj_lines}\n\n"
        f"Type numbers separated by commas.\n"
        f"e.g. `1,2,3` or `all` for every subject\n\n"
        f"_The bot will auto-discover all topics inside each selected subject "
        f"and create separate forum threads for each topic._",
        timeout=90
    )
    if not sp: return

    if sp.lower() == "all":
        chosen_subjects = all_subjects
    else:
        chosen_subjects = []
        for part in sp.split(","):
            part = part.strip()
            if part.isdigit() and 0 < int(part) <= len(all_subjects):
                chosen_subjects.append(all_subjects[int(part) - 1])

    if not chosen_subjects:
        await m.reply_text("❌ No valid subjects selected."); return

    # Confirm selection
    sel_names = "\n".join(f"  ✅ {s.get('subject_name', '?')}" for s in chosen_subjects)
    await m.reply_text(
        f"**Selected subjects ({len(chosen_subjects)}):**\n{sel_names}\n\n"
        f"_All topics inside these subjects will be tracked automatically._"
    )

    subjects_cfg = [
        {"subjectid": s.get("subjectid"), "subject_name": s.get("subject_name", "")}
        for s in chosen_subjects
    ]

    # ── Step 5: Test series ──────────────────────────────────────────────────
    inc_raw = await ask(
        "**[5/8] Include Test Series?**\n`yes` to also auto-download test series\n`no` to skip",
        timeout=30
    )
    inc_series = (inc_raw or "no").lower() in ("yes", "y", "1")

    # ── Step 6: Download type + quality ─────────────────────────────────────
    dl_pick = await ask(
        "**[6/8] Download Type**\n"
        "`1` — Videos only\n"
        "`2` — PDFs only\n"
        "`3` — Everything (videos + PDFs + quizzes)\n"
        "`4` — Quizzes only\n"
        "_(default: 3)_",
        timeout=30
    )
    dl_type = (dl_pick or "3").strip()
    if dl_type not in ("1", "2", "3", "4"):
        dl_type = "3"

    qual = await ask(
        "**Video Quality**\n`144p` `240p` `360p` `480p` `720p` `1080p`\n_(default: 480p)_",
        timeout=20
    )
    quality = (qual or "480p").strip()

    # ── Step 7: Schedule time ────────────────────────────────────────────────
    time_str = await ask(
        "**[7/8] Schedule Time (24-hr HH:MM)**\n"
        "e.g. `09:00`  `14:30`  `21:00`",
        timeout=60
    )
    if not time_str: return
    try:
        hh, mm = [int(x) for x in time_str.split(":")]
        assert 0 <= hh <= 23 and 0 <= mm <= 59
    except Exception:
        await m.reply_text("❌ Invalid time format. Use HH:MM."); return

    tz_raw = await ask(
        "**Timezone**\n"
        "e.g. `Asia/Kolkata`  `UTC`  `America/New_York`\n"
        "_(default: Asia/Kolkata)_",
        timeout=30
    )
    tz_name = (tz_raw or "Asia/Kolkata").strip()
    try:
        pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        tz_name = "Asia/Kolkata"

    repeat_raw = await ask(
        "**Repeat Pattern**\n"
        "`daily`     — every day\n"
        "`once`      — just once\n"
        "`mon`       — every Monday\n"
        "`mon,wed,fri` — specific days\n"
        "_(default: daily)_",
        timeout=30
    )
    repeat = (repeat_raw or "daily").strip().lower()

    # ── Step 8: Destination supergroup ──────────────────────────────────────
    sg_raw = await ask(
        "**[8/8] Destination Supergroup ID**\n"
        "Send the numeric ID (e.g. `-1001234567890`)\n\n"
        "⚠️ The bot must be **admin** in the group\n"
        "with **'Manage Topics'** permission enabled.\n"
        "Also enable **Topics** in group settings.",
        timeout=60
    )
    if not sg_raw: return
    if not sg_raw.lstrip("-").isdigit():
        await m.reply_text("❌ Invalid group ID."); return
    supergroup_id = int(sg_raw)

    # Brand + watermark
    brand_raw = await ask(
        "**Brand name for captions?**\nSend name or `/d` for default.",
        timeout=20
    )
    brand = CREDIT if (not brand_raw or brand_raw == "/d") else brand_raw

    wm_raw = await ask(
        "**Watermark text on videos?**\nSend text or `/d` to skip.",
        timeout=20
    )
    watermark = None if (not wm_raw or wm_raw == "/d") else wm_raw

    # ── Save to MongoDB ──────────────────────────────────────────────────────
    doc = {
        "user_id":             uid,
        "api_base":            api_base,
        "token":               token,
        "appx_user_id":        appx_uid,
        "course_id":           course_id,
        "course_name":         cname,
        "subjects":            subjects_cfg,
        "include_test_series": inc_series,
        "dl_type":             dl_type,
        "quality":             quality,
        "brand":               brand,
        "watermark":           watermark,
        "time":                f"{hh:02d}:{mm:02d}",
        "timezone":            tz_name,
        "repeat":              repeat,
        "supergroup_id":       supergroup_id,
        "active":              True,
        "created_at":          datetime.utcnow().isoformat(),
    }
    from db import db as _db
    sid = _db.save_schedule(doc)
    doc["_id"] = sid
    register_schedule(doc)

    snames = "  ".join(f"`{s['subject_name']}`" for s in subjects_cfg)
    await m.reply_text(
        f"✅ **Batch Schedule Saved!**\n\n"
        f"📚 Course: `{cname}`\n"
        f"📂 Subjects ({len(subjects_cfg)}): {snames}\n"
        f"📝 Test Series: `{'yes' if inc_series else 'no'}`\n"
        f"🎥 Type: `{dl_type}`  Quality: `{quality}`\n"
        f"🕐 Time: `{hh:02d}:{mm:02d}` ({tz_name})\n"
        f"🔁 Repeat: `{repeat}`\n"
        f"📤 Supergroup: `{supergroup_id}`\n\n"
        f"🆔 Schedule ID: `{sid}`\n\n"
        f"Use /schedules to view all batches\n"
        f"Use /runnow `{sid}` to test it immediately"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /schedules — list all batches
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_schedules(bot_client, m):
    from db import db as _db
    docs = _db.get_schedules(m.from_user.id)
    if not docs:
        await m.reply_text(
            "📭 No batches yet.\nUse /addschedule to create one.\n"
            "_(You can set up to 6 independent batches)_"
        )
        return

    lines = []
    for i, d in enumerate(docs, 1):
        subj_names = "  ".join(
            f"`{s['subject_name']}`" for s in d.get("subjects", [])
        )
        next_run = ""
        job = scheduler.get_job(f"sched_{d['_id']}")
        if job and job.next_run_time:
            next_run = f"\n   ⏭ Next: `{job.next_run_time.strftime('%d %b %H:%M %Z')}`"

        lines.append(
            f"**Batch {i}** — `{d.get('course_name', '?')}`\n"
            f"   📂 {subj_names}\n"
            f"   🕐 `{d.get('time')}` ({d.get('timezone')}) — `{d.get('repeat')}`\n"
            f"   📝 Series: `{'yes' if d.get('include_test_series') else 'no'}`"
            f"   🎥 `{d.get('dl_type')}` `{d.get('quality')}`\n"
            f"   📤 SG: `{d.get('supergroup_id')}`"
            f"{next_run}\n"
            f"   🆔 `{d['_id']}`"
        )

    await m.reply_text(
        f"📅 **Your Batches ({len(docs)}):**\n\n" +
        "\n\n".join(lines) +
        "\n\n`/delschedule <id>` — delete a batch\n"
        "`/runnow <id>` — run a batch right now"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /delschedule and /runnow
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_delschedule(bot_client, m):
    from db import db as _db
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.reply_text("Usage: `/delschedule <schedule_id>`"); return
    sid    = parts[1].strip()
    job_id = f"sched_{sid}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    ok = _db.delete_schedule(sid)
    await m.reply_text(
        f"🗑️ Batch `{sid}` deleted." if ok else f"❌ Not found: `{sid}`"
    )


async def cmd_runnow(bot_client, m):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.reply_text("Usage: `/runnow <schedule_id>`"); return
    sid = parts[1].strip()
    await m.reply_text(f"▶️ Running batch `{sid}` now…")
    asyncio.create_task(run_scheduled_job(sid))
