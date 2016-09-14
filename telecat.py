#!/usr/bin/env python
import os
import sys
import logging
import threading
import json
import time
import tempfile
from functools import wraps
import pyhashcat
try:
    from telegram.ext import Job
    from telegram.ext import Filters
    from telegram.ext import Updater
    from telegram.ext import CommandHandler
    from telegram.ext import MessageHandler
except ImportError:
    print("Please install python-telegram-bot")
    sys.exit(-1)

# TODO(gerry): Display speed in h/s
# TODO(gerry): Automatically stop status messages on session completion
# TODO(gerry): Fix out of sync stats (e.g., on /quit or completion)
# TODO(gerry): Check for successful execution on /launch
# TODO(gerry): Combine protected decorators

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_FILENAME = "config.json"
REQUESTED = []
USER_JOBS = {}
hashcat = None
stop_event, monitor_stop_event = threading.Event(), threading.Event()


def reject_user(bot, update):
    bot.send_message(chat_id=update.message.chat_id, text="403 Not Allowed.")


def admin_required(func):
    @wraps(func)
    def func_wrapper(bot, update, *args, **kwargs):
        user = update.message.from_user
        if user.id not in ADMINS:
            return reject_user(bot, update)
        return func(bot, update, *args, **kwargs)
    return func_wrapper


def watcher_required(func):
    @wraps(func)
    def func_wrapper(bot, update, *args, **kwargs):
        user = update.message.from_user
        if user.id not in ADMINS + WATCHERS:
            return reject_user(bot, update)
        return func(bot, update, *args, **kwargs)
    return func_wrapper


def start(bot, update):
    global REQUESTED
    user = update.message.from_user
    if user.id not in ADMINS + WATCHERS + REQUESTED:
        REQUESTED.append(user.id)
        bot.send_message(chat_id=update.message.chat_id, text="Request received.")
        bot.send_message(chat_id=ADMINS[0], text="New user request: %s(%s)" % (
            user.id, user.username))
        return
    bot.send_message(chat_id=update.message.chat_id, text="Nothing to do here.")


@watcher_required
def stats(bot, update, args, job_queue):
    global USER_JOBS
    help_msg = "To schedule automatic status messages:\n    /stats interval_in_seconds\n"
    help_msg += "To stop:\n    /stats STOP"
    msg = None
    if args:
        user = update.message.from_user
        if args[0].upper() == "STOP":
            job = USER_JOBS.get(user.id, None)
            if job:
                job.schedule_removal()
                bot.send_message(chat_id=update.message.chat_id,
                                 text="Disabled automatic status messages.")
                return
        elif args[0].isdigit():
            cur_job = USER_JOBS.get(user.id, None)
            if cur_job:
                cur_job.schedule_removal()
            job = Job(send_stats_job, float(args[0]), context=update.message.chat_id)
            USER_JOBS[user.id] = job
            job_queue.put(job)
            msg = "Automatic status messages %s." % (cur_job and 'updated' or 'started')
        else:
            msg = help_msg
        bot.send_message(chat_id=update.message.chat_id, text=msg)
    if hashcat:
        send_stats(bot, update.message.chat_id)
    elif msg is None:
        bot.send_message(chat_id=update.message.chat_id, text="Hashcat not running!")


def send_stats_job(bot, job):
    send_stats(bot, job.context)


def send_stats(bot, chat_id, session_complete=False):
    msg = ""
    if not hashcat:
        return
    if session_complete:
        msg = "*Session Complete*\n"
    if hashcat.stats:
        msg += format_stats(hashcat.stats, hashcat.command_line)
    if hashcat.output:
        msg += "*Output*\n`" + hashcat.output + "`"
    bot.sendMessage(chat_id=chat_id, text=msg, parse_mode="Markdown")


@admin_required
def pause(bot, update):
    msg = "Hashcat not running!"
    if hashcat and hashcat.process:
        msg = hashcat.pause() and "Paused" or "Failed to pause!"
    bot.send_message(chat_id=update.message.chat_id, text=msg)


@admin_required
def resume(bot, update):
    msg = "Hashcat not running!"
    if hashcat and hashcat.process:
        msg = hashcat.resume() and "Resume" or "Failed to resume!"
    bot.send_message(chat_id=update.message.chat_id, text=msg)


@admin_required
def quit(bot, update):
    msg = "Hashcat not running!"
    if hashcat and hashcat.process:
        msg = "Quiting hashcat session."
        hashcat.quit()
    bot.send_message(chat_id=update.message.chat_id, text=msg)


@admin_required
def launch(bot, update, args):
    global hashcat
    if hashcat and hashcat.process and hashcat.process.poll() is None:
        return bot.send_message(chat_id=update.message.chat_id, text="Hashcat is already running!")

    msg = "We need some args!"
    if args:
        hashcat = pyhashcat.HashcatController(args, stop_event)
        t = threading.Thread(target=hashcat.run)
        t.setDaemon(True)
        t.start()
        t = threading.Thread(target=session_monitor, args=(bot, hashcat, monitor_stop_event))
        t.setDaemon(True)
        t.start()
        msg = "Launched a new scan: `%s`" % " ".join(hashcat.command_line)
    logger.info(msg)
    bot.send_message(chat_id=update.message.chat_id, text=msg, parse_mode="Markdown")


def unknown(bot, update):
    if update.message.from_user.id in ADMINS + WATCHERS:
        bot.send_message(chat_id=update.message.chat_id,
                         text="Sorry, I didn't understand that command.")


@admin_required
def receive_file(bot, update):
    doc = update.message.document
    if doc.mime_type != 'text/plain':
        bot.send_message(chat_id=update.message.chat_id,
                         text="Sorry, only 'text/plain' files are supported.")
        return
    logging.info('%s sent a file: %s' % (update.message.from_user.username, doc.file_name))
    infile = bot.get_file(doc.file_id)
    upload_dir = os.path.expanduser(DOWNLOAD_PATH)
    prefix = "%s_" % update.message.from_user.username
    (fd, file_name) = tempfile.mkstemp(prefix=prefix, dir=upload_dir)
    infile.download(file_name)
    bot.send_message(chat_id=update.message.chat_id,
                     text="File saved as %s" % file_name)
    logging.info("File saved as %s" % file_name)


def error(bot, update, error):
    logger.warn('Update "%s" caused error "%s"' % (update, error))


def session_monitor(bot, hashcat, cancel_event, sleep_time=5):
    while not cancel_event.is_set():
        if hashcat.process and hashcat.process.poll() is not None:
            logger.info("Session completed, waiting for handler to finish...")
            hashcat.stop_event.wait(60)
            logger.info("Notifing watchers.")
            for user_id in WATCHERS + ADMINS:
                send_stats(bot, user_id, session_complete=True)
            break
        time.sleep(sleep_time)


def format_stats(stats, cmd_line):
    msg = ["*Current Status:* `%s`" % pyhashcat.STATUS_CODES[stats.get('STATUS')]]
    gpus = []
    for idx, gpu in enumerate(stats.get('SPEED', [])):
        gpus.append("\tDevice: `%d`\tCount: `%d`\tms: `%f`" % (idx, gpu[0], gpu[1]))
    msg += ["*Current Speeds:*\n%s" % "\n".join(gpus)]
    msg += ["*Current Keyspace Unit:* `%s`" % stats.get('CURKU')]
    msg += ["*Progress:* `%d/%d`" % stats.get('PROGRESS')]
    msg += ["*Recovered Hashes:* `%d/%d`" % stats.get('RECHASH')]
    msg += ["*Recovered Salts:* `%d/%d`" % stats.get('RECSALT')]
    if stats.get('TEMP'):
        temps = []
        for idx, temp in enumerate(stats.get('TEMP', [])):
            temps.append("\tDevice: `%d`\tTemp: `%d`" % (idx, temp))
        msg += ["*Tempratures:*\n%s" % "\n".join(temps)]
    runtimes = []
    for idx, rt in enumerate(stats.get('EXEC_RUNTIME', [])):
        runtimes.append("\tDevice: `%d`\tms: `%f`" % (idx, rt))
    msg += ["*Runtimes:*\n%s" % "\n".join(runtimes)]
    msg += ["*Command Line:*\n\t`%s`" % " ".join(cmd_line)]
    return "\n".join(msg)


def load_config(config_filename=CONFIG_FILENAME):
    global ADMINS
    global BOT_TOKEN
    global WATCHERS
    global DOWNLOAD_PATH

    with open(config_filename) as config_file:
        config = json.load(config_file)
        BOT_TOKEN = config.get('BOT_TOKEN')
        ADMINS = config.get('admins')
        WATCHERS = config.get('watchers')
        DOWNLOAD_PATH = config.get('download_path', './uploads')
        return config


def main():
    config = load_config()
    if not config:
        logger.error("No config file could be loaded.")
        sys.exit(-1)

    if not BOT_TOKEN:
        logger.error("No BOT_TOKEN defined.")
        sys.exit(-1)

    updater = Updater(token=BOT_TOKEN)

    dp = updater.dispatcher
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('stats', stats, pass_args=True, pass_job_queue=True))
    dp.add_handler(CommandHandler('pause', pause))
    dp.add_handler(CommandHandler('resume', resume))
    dp.add_handler(CommandHandler('quit', quit))
    dp.add_handler(CommandHandler('launch', launch, pass_args=True))
    dp.add_handler(MessageHandler([Filters.command], unknown))
    dp.add_handler(MessageHandler([Filters.document], receive_file))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
