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
    from telegram.ext.dispatcher import run_async
except ImportError:
    print("Please install python-telegram-bot")
    sys.exit(-1)

# TODO(gerry): Rework session_monitor/send_stats/send_stats_job to use hashcat.is_running()
#              or hashcat.process.returncode/status/stop_event
# TODO(gerry): Add usage/help command
# TODO(gerry): Combine protected decorators

logging.basicConfig(level=logging.DEBUG,
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
        if user.id not in config.get('admins'):
            return reject_user(bot, update)
        return func(bot, update, *args, **kwargs)
    return func_wrapper


def watcher_required(func):
    @wraps(func)
    def func_wrapper(bot, update, *args, **kwargs):
        user = update.message.from_user
        if user.id not in config.get('admins') + config.get('watchers'):
            return reject_user(bot, update)
        return func(bot, update, *args, **kwargs)
    return func_wrapper


def start(bot, update):
    global REQUESTED
    user = update.message.from_user
    if user.id not in config.get('admins') + config.get('watchers') + REQUESTED:
        REQUESTED.append(user.id)
        bot.send_message(chat_id=update.message.chat_id, text="Request received.")
        bot.send_message(chat_id=config.get('admins')[0], text="New user request: %s(%s)" % (
            user.id, user.username))
        return

    if not hashcat.is_running():
        return bot.send_message(chat_id=update.message.chat_id, text="No current sessions")
    msg = "*Current Status:*\n" + format_stats(hashcat.stats, hashcat.command_line)
    bot.send_message(chat_id=update.message.chat_id, text=msg, parse_mode="Markdown")


@watcher_required
def stats(bot, update, args, job_queue):
    global USER_JOBS
    help_msg = "To schedule automatic status messages:\n    /stats interval_in_mins\n"
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
            job = Job(send_stats_job, float(args[0])*60, context=update.message.chat_id)
            USER_JOBS[user.id] = job
            job_queue.put(job)
            msg = "Automatic status messages %s." % (cur_job and 'updated' or 'started')
        else:
            msg = help_msg
        bot.send_message(chat_id=update.message.chat_id, text=msg)
    if hashcat.is_running():
        send_stats(bot, update.message.chat_id)
    elif msg is None:
        bot.send_message(chat_id=update.message.chat_id, text="Hashcat not running!")


def send_stats_job(bot, job):
    if hashcat.process and hashcat.process.poll() is not None:
        job.schedule_removal()
    elif hashcat.stats:
        send_stats(bot, job.context)


def send_stats(bot, chat_id, session_complete=False):
    msg = ""
    if session_complete:
        msg = "*Session Complete*\n"
    if hashcat.stats:
        msg += format_stats(hashcat.stats, hashcat.command_line)
    if hashcat.output:
        msg += "*Output:*\n`" + hashcat.output + "`"
    if hashcat.error_output:
        msg += "*Error:*\n`" + hashcat.error_output + "`"
    if len(msg):
        bot.sendMessage(chat_id=chat_id, text=msg, parse_mode="Markdown")


@admin_required
def pause(bot, update):
    if not hashcat.is_running():
        msg = "Hashcat not running!"
    elif hashcat.is_paused():
        msg = "Already paused."
    else:
        msg = hashcat.pause() and "Paused" or "Failed to pause!"
    bot.send_message(chat_id=update.message.chat_id, text=msg)


@admin_required
def resume(bot, update):
    if not hashcat.is_running():
        msg = "Hashcat not running!"
    elif hashcat.is_paused():
        msg = "Hashcat not paused."
    else:
        msg = hashcat.resume() and "Resumed" or "Failed to resume!"
    bot.send_message(chat_id=update.message.chat_id, text=msg)


@admin_required
def quit(bot, update):
    if not hashcat.is_running():
        msg = "Hashcat not running!"
    else:
        msg = "Quiting hashcat session."
        hashcat.quit()
    bot.send_message(chat_id=update.message.chat_id, text=msg)


@admin_required
def launch(bot, update, args, job_queue):
    if hashcat.is_running():
        return bot.send_message(chat_id=update.message.chat_id,
                                text="Hashcat is already running!")

    msg = "We need a command line!"
    if args:
        run_async(hashcat.run)(args)
        run_async(session_monitor)(bot)
        
        while not hashcat.is_running():
            if hashcat.process and hashcat.process.returncode:
                break
        if hashcat.process.returncode:
            bot.send_message(chat_id=update.message.chat_id,
                             text="Error launching new scan!", parse_mode="Markdown")
            return send_stats(bot, update.message.chat_id)
        else:
            msg = "Launched a new scan: `%s`" % " ".join(hashcat.command_line)
    logger.info(msg)
    bot.send_message(chat_id=update.message.chat_id, text=msg, parse_mode="Markdown")


def unknown(bot, update):
    if update.message.from_user.id in config.get('admins') + config.get('watchers'):
        bot.send_message(chat_id=update.message.chat_id,
                         text="Sorry, I didn't understand that command.")


@admin_required
def receive_file(bot, update):
    doc = update.message.document
    if doc.mime_type != 'text/plain':
        return bot.send_message(chat_id=update.message.chat_id,
                                text="Sorry, only 'text/plain' files are supported.")
    logging.info('%s sent a file: %s' % (update.message.from_user.username, doc.file_name))
    infile = bot.get_file(doc.file_id)
    upload_dir = os.path.expanduser(config.get('download_path', ''))
    prefix = "%s_" % update.message.from_user.username
    (fd, file_name) = tempfile.mkstemp(prefix=prefix, dir=upload_dir)
    infile.download(file_name)
    bot.send_message(chat_id=update.message.chat_id,
                     text="File saved as %s" % file_name)
    logging.info("File saved as %s" % file_name)


def error(bot, update, error):
    logger.warn('Update "%s" caused error "%s"' % (update, error))


def session_monitor(bot, sleep_time=5):
    while not monitor_stop_event.is_set():
        # TODO(gerry): use is_runnning and STATE
        if hashcat.process and hashcat.process.poll() is not None:
            logger.info("Session completed, waiting for handler to finish...")
            hashcat.stop_event.wait(60)
            logger.info("Notifing config.get('watchers').")
            for user_id in config.get('watchers') + config.get('admins'):
                send_stats(bot, user_id, session_complete=True)
            return
        time.sleep(sleep_time)


def format_stats(stats, cmd_line):
    msg = ["*Current Status:* `%s`" % pyhashcat.STATUS_CODES[stats.get('STATUS')]]
    gpus = []
    for idx, gpu in enumerate(stats.get('SPEED', [])):
        hs = int((min(gpu[0], gpu[1]) > 0 and gpu[0]/gpu[1] or 0) * 1000)
        gpus.append("\tDevice: `%d`\tCount: `%d`\tms: `%f`\th/s: `%d`" % (idx, gpu[0], gpu[1], hs))
    msg += ["*Current Speeds:*\n%s" % "\n".join(gpus)]
    msg += ["*Current Keyspace Unit:* `%s`" % stats.get('CURKU')]
    prog = stats.get('PROGRESS')
    msg += ["*Progress:* `%.2f%% (%d/%d)`" % ((float(prog[0])/float(prog[1])*100,) + prog)]
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
    with open(config_filename) as config_file:
        return json.load(config_file)


def main():
    global hashcat
    global config

    config = load_config()
    if not config:
        logger.error("No config file could be loaded.")
        sys.exit(-1)

    if not config.get('BOT_TOKEN'):
        logger.error("No BOT_TOKEN defined.")
        sys.exit(-1)

    hashcat = pyhashcat.HashcatController(stop_event)
    updater = Updater(token=config.get('BOT_TOKEN'))

    dp = updater.dispatcher
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('stats', stats, pass_args=True, pass_job_queue=True))
    dp.add_handler(CommandHandler('pause', pause))
    dp.add_handler(CommandHandler('resume', resume))
    dp.add_handler(CommandHandler('quit', quit))
    dp.add_handler(CommandHandler('launch', launch, pass_args=True, pass_job_queue=True))
    dp.add_handler(MessageHandler([Filters.command], unknown))
    dp.add_handler(MessageHandler([Filters.document], receive_file))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
