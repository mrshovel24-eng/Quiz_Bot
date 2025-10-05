# main.py
import asyncio
import logging
import os
import random
import re
import certifi
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from aiohttp import web  # for web server
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from bson.objectid import ObjectId

# Load environment
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
REPORT_CHANNEL_ID = int(os.getenv('REPORT_CHANNEL_ID')) if os.getenv('REPORT_CHANNEL_ID') else None
CHANNEL_TO_JOIN = int(os.getenv('CHANNEL_TO_JOIN')) if os.getenv('CHANNEL_TO_JOIN') else None

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot and Dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# MongoDB
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
SYSTEM_DBS = {"admin","local","config","_quiz_meta_"}
meta_db = client["_quiz_meta_"]
user_progress_col = meta_db["user_progress"]
user_results_col = meta_db["user_results"]

try:
    client.admin.command('ping')
    logger.info("✅ MongoDB connected")
except Exception as e:
    logger.exception(f"❌ MongoDB connection failed: {e}")

# ---------------- States ----------------
class QuizStates(StatesGroup):
    waiting_for_ready = State()
    selecting_subject = State()
    selecting_topic = State()
    answering_quiz = State()
    post_quiz = State()
    reporting_issue = State()

# ---------------- Helpers ----------------
def chunked(lst: List[Any], n: int):
    return [lst[i:i+n] for i in range(0,len(lst),n)]

def sanitize_question_doc(q: Dict[str,Any]) -> Dict[str,Any]:
    return {k: str(v) if isinstance(v,ObjectId) else v for k,v in q.items()}

def start_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Start Quiz", callback_data="ready")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="help")],
    ])

def create_inline_keyboard(button_texts, prefix, row_width=2):
    buttons = [InlineKeyboardButton(text=text, callback_data=f"{prefix}:{text}") for text in button_texts]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i+row_width] for i in range(0,len(buttons),row_width)])

def list_user_dbs():
    try:
        return [db for db in client.list_database_names() if db not in SYSTEM_DBS]
    except ConnectionFailure:
        return []

def list_collections(dbname: str):
    try:
        return client[dbname].list_collection_names()
    except Exception as e:
        logger.exception(f"Collections error: {e}")
        return []

def clean_question_text(text: str):
    return re.sub(r"^\s*\d+\.\s*","",(text or "")).strip()

def fetch_nonrepeating_questions(dbname, colname, user_id, n=10):
    try:
        prog_key = {"user_id":user_id,"db":dbname,"collection":colname or "_RANDOM_"}
        doc = user_progress_col.find_one(prog_key) or {}
        served = set(doc.get("served_qids",[]))
        results, pool = [], []

        if colname:
            cursor = client[dbname][colname].find({})
            for d in cursor:
                qid = d.get("question_id") or str(d.get("_id"))
                if qid not in served: pool.append(d)
        else:
            for cname in list_collections(dbname):
                cursor = client[dbname][cname].find({})
                for d in cursor:
                    qid = d.get("question_id") or str(d.get("_id"))
                    if qid not in served: pool.append(d)

        if not pool: return []
        random.shuffle(pool)
        for q in pool:
            qid = q.get("question_id") or str(q.get("_id"))
            if qid in served: continue
            served.add(qid)
            results.append(sanitize_question_doc(q))
            if len(results)>=n: break

        user_progress_col.update_one(prog_key, {"$set":{"served_qids":list(served)}}, upsert=True)
        return results[:n]
    except Exception as e:
        logger.exception(f"Fetching error: {e}")
        return []

def get_correct_answer(q):
    try:
        raw = (q.get('answer') or q.get('correct') or "").strip().lower()
        if raw in ('a','b','c','d'): return raw
        if raw.isdigit(): return {'1':'a','2':'b','3':'c','4':'d'}.get(raw,'a')
        m = re.search(r'([abcd])',raw)
        return m.group(1) if m else 'a'
    except: return 'a'

    return random.choice(["Great job! 💪","Nice! 🚀","Well done! 🔥","Progress over perfection ✅"])

async def is_channel_member(user_id):
    try:
        if CHANNEL_TO_JOIN is None: return True
        member = await bot.get_chat_member(CHANNEL_TO_JOIN, user_id)
        return member.status in ['member','administrator','creator']
    except: return False

# ---------------- Handlers ----------------
@dp.message(Command(commands=['start']))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(subject_locked=False, topic_locked=False, quiz_locked=False)
    if await is_channel_member(message.from_user.id):
        msg = await message.reply("🎉 Welcome! Press Start Quiz.", reply_markup=start_menu())
        await state.update_data(menu_msg_id=msg.message_id)
        await state.set_state(QuizStates.waiting_for_ready)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("🔗 Join Now", url="https://t.me/usersforstudy")],
            [InlineKeyboardButton("✅ Try Again", callback_data="try_again")]
        ])
        await message.reply("🔒 Join channel first.", reply_markup=kb)

@dp.callback_query(F.data=="help")
async def help_callback(callback: CallbackQuery):
    help_text =("📖 *Quiz Bot – How It Works*\n\n"
    "👋 Welcome! This bot is designed to help you practice quizzes and track your progress. Here's how it works:\n\n"
    
    "1️⃣ *Start the Bot*\n"
    "Press *Start Quiz* to begin. Make sure to join channel to begin the Quiz.\n\n"
    
    "2️⃣ *Select a Subject*\n"
    "Choose the subject you want to practice from the list.\n\n"
    
    "3️⃣ *Select a Topic*\n"
    "Choose a specific topic within that subject, or select *🎲 Random* to get questions from all topics.\n\n"
    
    "4️⃣ *Answer Quiz Questions*\n"
    "You will get *10 questions* per quiz.\n"
    "Each question appears as a single interactive message with multiple-choice options.\n"
    "Once you answer a question, the next one appears and replaces the previous message.\n"
    "⏱ All questions are available for *10 minutes* only.\n\n"
    
    "5️⃣ *Quiz Completion*\n"
    "After answering all questions, your *score* will be shown.\n"
    "You can then *Start Again* or *Report an Issue* if needed.\n\n"
    
    "6️⃣ *Report an Issue*\n"
    "Send a screenshot or describe the problem.\n"
    "The bot will forward your report to the admin channel for support.\n\n"
    )
    await callback.message.reply(help_text, parse_mode="Markdown",reply_markup=start_menu())
    await callback.answer()

@dp.callback_query(F.data=="try_again")
async def try_again_callback(callback: CallbackQuery, state: FSMContext):
    if await is_channel_member(callback.from_user.id):
        await callback.message.edit_text("🎉 Welcome! Press Start Quiz.", reply_markup=start_menu())
        await state.set_state(QuizStates.waiting_for_ready)
    else:
        await callback.answer("Not joined yet.", show_alert=True)

@dp.callback_query(QuizStates.waiting_for_ready, F.data=="ready")
async def ready_callback(callback: CallbackQuery, state: FSMContext):
    subjects = list_user_dbs()
    if not subjects:
        await callback.message.reply("❌ No subjects.")
        await state.clear()
        return
    await state.update_data(subject_locked=False)
    subject_keyboard = create_inline_keyboard(subjects,"subject",2)
    await callback.message.edit_text("📚 Select a subject:", reply_markup=subject_keyboard)
    await state.set_state(QuizStates.selecting_subject)

@dp.callback_query(QuizStates.selecting_subject, F.data.startswith("subject:"))
async def subject_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("subject_locked"): 
        await callback.answer("⚠️ Finish current subject first!", show_alert=True)
        return
    await callback.answer()
    subject = callback.data.split(":",1)[1]
    await state.update_data(subject=subject, subject_locked=True)
    topics = list_collections(subject)
    if not topics:
        await callback.message.edit_text("❌ No topics.")
        await state.clear()
        return
    topic_buttons = ["🎲 Random"]+topics
    topic_keyboard = create_inline_keyboard(topic_buttons,"topic",2)
    await callback.message.edit_text("📖 Select a topic:", reply_markup=topic_keyboard)
    await state.set_state(QuizStates.selecting_topic)

@dp.callback_query(QuizStates.selecting_topic, F.data.startswith("topic:"))
async def topic_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("topic_locked"):
        await callback.answer("⚠️ Quiz in progress!", show_alert=True)
        return
    await callback.answer()
    topic = callback.data.split(":",1)[1]
    subject = data.get("subject")
    actual_topic = None if topic=="🎲 Random" else topic
    questions = fetch_nonrepeating_questions(subject, actual_topic, callback.from_user.id, n=10)
    if not questions:
        await callback.answer("❌ Not enough questions", show_alert=True)
        await state.clear()
        return
    await state.update_data(
        topic=actual_topic,
        topic_display=topic,
        questions=questions,
        current_question=0,
        score=0,
        quiz_chat_id=callback.message.chat.id,
        topic_locked=True,
        quiz_locked=True
    )
    await callback.message.edit_text(f"🚀 Starting quiz: {subject} - {topic}", reply_markup=None)
    await send_next_question(bot, callback.message.chat.id, state)

# ---------------- Quiz Flow ----------------
async def send_next_question(bot: Bot, chat_id:int, state:FSMContext):
    data = await state.get_data()
    questions = data.get("questions",[])
    current = int(data.get("current_question",0))
    if current>=len(questions):
        score = int(data.get("score",0))
        total = len(questions)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Start Again", callback_data="start_again")],
            [InlineKeyboardButton(text="📢 Report Issue", callback_data="report_issue")]

        ])
        nk = "📢 Forward these questions if you want!\n\n⏱️ These questions will auto-delete after 5 minutes"
        await bot.send_message(
                             chat_id, 
    f"🎉 Quiz finished!\n✅ Correct: {score}\n❌ Wrong: {total-score}\n\n{nk}", 
    reply_markup=kb
)

        await state.set_state(QuizStates.post_quiz)
        await state.update_data(subject_locked=False, topic_locked=False, quiz_locked=False)
        return
    q = questions[current]
    opts = [q.get("option_a",""), q.get("option_b",""), q.get("option_c",""), q.get("option_d","")]
    qtext = clean_question_text(q.get("question") or q.get("text"))
    correct_index = {"a":0,"b":1,"c":2,"d":3}.get(get_correct_answer(q),0)
    poll_msg = await bot.send_poll(chat_id, question=f"Q{current+1}: {qtext}", options=opts,
                                   type="quiz", correct_option_id=correct_index, is_anonymous=False)
    await state.update_data(current_poll_id=poll_msg.poll.id)
    # Auto delete after 10 minutes
    asyncio.create_task(auto_delete(poll_msg.message_id, chat_id, 300))

async def auto_delete(message_id, chat_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except: pass

@dp.poll_answer()
async def poll_answer_handler(poll: types.PollAnswer, state:FSMContext):
    data = await state.get_data()
    if poll.poll_id != data.get("current_poll_id"): return
    questions = data.get("questions",[])
    current = int(data.get("current_question",0))
    if current>=len(questions): return
    q = questions[current]
    correct_index = {"a":0,"b":1,"c":2,"d":3}.get(get_correct_answer(q),0)
    chosen = poll.option_ids[0] if poll.option_ids else -1
    score = int(data.get("score",0)) + (1 if chosen==correct_index else 0)
    await state.update_data(current_question=current+1, score=score)
    chat_id = data.get("quiz_chat_id")
    await asyncio.sleep(1)
    await send_next_question(bot, chat_id, state)

# ---------------- Post Quiz ----------------
@dp.callback_query(QuizStates.post_quiz, F.data=="start_again")
async def start_again_callback(callback: CallbackQuery, state:FSMContext):
    await callback.answer()
    await ready_callback(callback, state)

@dp.callback_query(QuizStates.post_quiz, F.data=="report_issue")
async def report_issue_callback(callback: CallbackQuery, state:FSMContext):
    await callback.answer()
    await callback.message.reply("📷 Send screenshot or describe the issue.")
    await state.set_state(QuizStates.reporting_issue)

@dp.message(QuizStates.reporting_issue)
async def report_issue_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    try:
        if REPORT_CHANNEL_ID is None:
            await message.reply("❌ Reporting channel not set.")
            return

        # Forward text messages
        if message.text:
            text = f"🚨 Issue from @{username} (ID:{user_id}):\n{message.text}"
            await bot.send_message(REPORT_CHANNEL_ID, text)

        # Forward photos
        elif message.photo:
            photo = message.photo[-1]
            await bot.send_photo(REPORT_CHANNEL_ID, photo.file_id, caption=f"🚨 Issue from @{username}")

        # Forward polls
        elif message.poll:
            poll = message.poll
            options = [opt.text for opt in poll.options]
            correct_id = poll.correct_option_id if poll.type == "quiz" else None
            await bot.send_poll(
                REPORT_CHANNEL_ID,
                question=f"🚨 Issue from @{username}: {poll.question}",
                options=options,
                type=poll.type,
                correct_option_id=correct_id,
                is_anonymous=poll.is_anonymous
            )

        await message.reply("✅ Report sent!", reply_markup=start_menu())
    except Exception as e:
        logger.exception(f"Failed to forward report: {e}")
        await message.reply("❌ Failed to send report.")
    
    await state.update_data(quiz_locked=False)
    await state.clear()

    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    try:
        if message.text:
            text = f"🚨 Issue from @{username} (ID:{user_id}):\n{message.text}"
            if REPORT_CHANNEL_ID: await bot.send_message(REPORT_CHANNEL_ID, text)
        elif message.photo:
            photo = message.photo[-1]
            if REPORT_CHANNEL_ID: await bot.send_photo(REPORT_CHANNEL_ID, photo.file_id, caption=f"🚨 Issue from @{username}")
        await message.reply("✅ Report sent!", reply_markup=start_menu())
    except: await message.reply("❌ Failed to send report")
    await state.update_data(quiz_locked=False)
    await state.clear()
async def alive_checker():
    while True:
        try:
            if REPORT_CHANNEL_ID:
                await bot.send_message(REPORT_CHANNEL_ID, f"🤖 I am alive! Time:")
        except Exception as e:
            logger.exception("Error sending alive message")
        await asyncio.sleep(300)  # 5 minutes

# ✅ NEW: aiohttp minimal web app
app = web.Application()

async def handle(request):
    return web.Response(text="Bot is alive")

app.router.add_get("/", handle)

# Main function to run bot + web server
async def main():
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting web server on port {port}")

    # Start aiohttp web server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("🌍 Web server started successfully")

    # Clear webhook (important if deploying after webhook setup)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🧹 Webhook cleared")

    # Start background alive checker
    asyncio.create_task(alive_checker())

    # Start bot polling in parallel
    asyncio.create_task(dp.start_polling(bot))
    logger.info("🤖 Bot polling started")

    # Keep running forever
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
