import os
import time
import tempfile
import instaloader
import requests
from pymongo import MongoClient
from bson.binary import Binary
from pathlib import Path
from typing import Set, Optional
from datetime import datetime
from threading import Thread, Lock
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from dotenv import load_dotenv  # Make sure to install python-dotenv

# === LOAD ENVIRONMENT VARIABLES ===
load_dotenv()

# === CONFIGURATION ===
MONGO_URI = os.getenv("MONGO_URI", "mongodb://root:secret@localhost:27017/insta_monitor?authSource=admin")
DB_NAME = os.getenv("DB_NAME", "insta_monitor")
SESSION_USERNAME = os.getenv("SESSION_USERNAME", "moe.mpg")
USERNAMES = os.getenv("USERNAMES", "jaqxul,ssh.daemon").split(",")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === DATABASE SETUP ===
mongo = MongoClient(MONGO_URI)
db = mongo[DB_NAME]
sessions_col = db["sessions"]
stories_col = db["stories"]


class SessionManager:
    """Handle Instagram session persistence via MongoDB."""
    
    def __init__(self, username: str):
        self.username = username
        self.temp_path = None
    
    def load(self) -> str:
        """Load session from MongoDB to temporary file."""
        doc = sessions_col.find_one({"username": self.username})
        if not doc:
            raise Exception(f"No session found for {self.username} in MongoDB.")
        
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".session")
        self.temp_path = temp_file.name
        temp_file.close()
        
        with open(self.temp_path, "wb") as f:
            f.write(doc["session_data"])
        
        print(f"✅ Loaded session for {self.username}")
        return self.temp_path
    
    def save(self):
        """Save session back to MongoDB."""
        if not self.temp_path or not os.path.exists(self.temp_path):
            return
        
        with open(self.temp_path, "rb") as f:
            data = f.read()
        
        sessions_col.update_one(
            {"username": self.username},
            {"$set": {"session_data": Binary(data)}},
            upsert=True
        )
    
    def cleanup(self):
        """Remove temporary session file."""
        if self.temp_path and os.path.exists(self.temp_path):
            try:
                os.remove(self.temp_path)
            except Exception as e:
                print(f"Warning: Could not remove temp session file: {e}")


class StoryTracker:
    """Track which stories have been seen/sent."""
    
    @staticmethod
    def get_seen_stories(username: str) -> Set[str]:
        doc = stories_col.find_one({"username": username})
        return set(doc.get("seen_ids", [])) if doc else set()
    
    @staticmethod
    def mark_seen(username: str, story_id: str):
        stories_col.update_one(
            {"username": username},
            {"$addToSet": {"seen_ids": story_id}},
            upsert=True
        )
        print(f"📝 Marked story {story_id} as seen for {username}")


class TelegramSender:
    """Handle sending media to Telegram."""
    
    def __init__(self, bot_token: str, chat_id: str):
        if not bot_token or not chat_id:
            raise ValueError("Missing Telegram bot token or chat ID.")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    def _convert_webp_to_jpg(self, webp_path: str) -> Optional[str]:
        try:
            from PIL import Image
            jpg_path = webp_path.rsplit('.', 1)[0] + '_converted.jpg'
            with Image.open(webp_path) as img:
                if img.mode in ('RGBA', 'LA', 'P'):
                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    rgb_img.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    rgb_img.save(jpg_path, 'JPEG', quality=95)
                else:
                    img.convert('RGB').save(jpg_path, 'JPEG', quality=95)
            print(f"🔄 Converted WebP to JPG: {os.path.basename(jpg_path)}")
            return jpg_path
        except ImportError:
            print("⚠️ PIL/Pillow not installed. Install with: pip install Pillow")
            return None
        except Exception as e:
            print(f"⚠️ WebP conversion failed: {e}")
            return None
    
    def send_file(self, file_path: str, caption: str = "") -> bool:
        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}")
            return False
        
        ext = os.path.splitext(file_path)[1].lower()
        converted_file = None
        
        if ext == ".webp":
            converted_file = self._convert_webp_to_jpg(file_path)
            if converted_file:
                file_path = converted_file
                ext = ".jpg"
            else:
                endpoint = "sendDocument"
                file_param = "document"
                url = f"{self.base_url}/{endpoint}"
                try:
                    with open(file_path, "rb") as f:
                        files = {file_param: f}
                        data = {"chat_id": self.chat_id}
                        if caption:
                            data["caption"] = caption
                        resp = requests.post(url, data=data, files=files, timeout=60)
                    return resp.ok
                except Exception as e:
                    print(f"❌ Failed to send to Telegram: {e}")
                    return False
        
        if ext in [".jpg", ".jpeg", ".png", ".gif"]:
            endpoint = "sendPhoto"
            file_param = "photo"
        elif ext in [".mp4", ".mov", ".avi", ".mkv"]:
            endpoint = "sendVideo"
            file_param = "video"
        else:
            endpoint = "sendDocument"
            file_param = "document"
        
        url = f"{self.base_url}/{endpoint}"
        
        try:
            with open(file_path, "rb") as f:
                files = {file_param: f}
                data = {"chat_id": self.chat_id}
                if caption:
                    data["caption"] = caption
                resp = requests.post(url, data=data, files=files, timeout=60)
            if resp.ok:
                print(f"✅ Sent to Telegram: {os.path.basename(file_path)}")
                return True
            else:
                print(f"⚠️ Telegram API error: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            print(f"❌ Failed to send to Telegram: {e}")
            return False
        finally:
            if converted_file and os.path.exists(converted_file):
                try:
                    os.remove(converted_file)
                except Exception as e:
                    print(f"⚠️ Could not remove converted file: {e}")


class StoryDownloader:
    """Download and manage Instagram stories."""
    
    def __init__(self, loader: instaloader.Instaloader, download_dir: str = "/tmp/insta_stories"):
        self.loader = loader
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
    
    def download_story(self, item, username: str) -> Optional[str]:
        story_id = str(item.mediaid)
        user_dir = self.download_dir / username
        user_dir.mkdir(exist_ok=True)
        existing_files = set(user_dir.glob("*"))
        
        try:
            old_pattern = self.loader.dirname_pattern
            self.loader.dirname_pattern = str(user_dir)
            self.loader.download_storyitem(item, target=username)
            self.loader.dirname_pattern = old_pattern
        except Exception as e:
            print(f"❌ Download failed for story {story_id}: {e}")
            return None
        
        new_files = set(user_dir.glob("*")) - existing_files
        
        if not new_files:
            print(f"⚠️ No new files found after download for story {story_id}")
            possible_files = list(user_dir.glob(f"*{story_id}*"))
            if possible_files:
                return str(max(possible_files, key=lambda p: p.stat().st_mtime))
            return None
        
        media_files = [
            f for f in new_files 
            if f.suffix.lower() in ['.jpg', '.jpeg', '.png', '.mp4', '.mov']
        ]
        
        if not media_files:
            media_files = list(new_files)
        
        if media_files:
            largest_file = max(media_files, key=lambda f: f.stat().st_size)
            print(f"📥 Downloaded: {largest_file.name}")
            return str(largest_file)
        return None


class StoryMonitor:
    """Main monitor that ties everything together."""
    
    def __init__(self):
        self.session_manager = SessionManager(SESSION_USERNAME)
        self.telegram = TelegramSender(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        session_path = self.session_manager.load()
        self.loader = instaloader.Instaloader(
            download_video_thumbnails=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False
        )
        self.loader.load_session_from_file(SESSION_USERNAME, session_path)
        self.downloader = StoryDownloader(self.loader)
    
    def check_user_stories(self, username: str):
        print(f"\n🔍 Checking stories for @{username}...")
        seen_stories = StoryTracker.get_seen_stories(username)
        
        try:
            profile = instaloader.Profile.from_username(self.loader.context, username)
            stories = self.loader.get_stories(userids=[profile.userid])
            
            new_count = 0
            for story in stories:
                for item in story.get_items():
                    story_id = str(item.mediaid)
                    if story_id in seen_stories:
                        continue
                    print(f"📸 New story found: {story_id}")
                    file_path = self.downloader.download_story(item, username)
                    if file_path:
                        caption = f"Story from @{username}"
                        success = self.telegram.send_file(file_path, caption)
                        if success:
                            new_count += 1
                            try:
                                os.remove(file_path)
                            except Exception as e:
                                print(f"⚠️ Could not remove file: {e}")
                        else:
                            print(f"⚠️ Failed to send story {story_id}")
                    StoryTracker.mark_seen(username, story_id)
            
            if new_count > 0:
                print(f"✨ Sent {new_count} new story/stories from @{username}")
            else:
                print(f"✓ No new stories from @{username}")
        except instaloader.exceptions.ProfileNotExistsException:
            print(f"❌ Profile does not exist: @{username}")
        except instaloader.exceptions.LoginRequiredException:
            print(f"❌ Login required - session may have expired")
        except Exception as e:
            print(f"❌ Error checking @{username}: {e}")
    
    def run_check_cycle(self):
        print(f"\n{'='*50}")
        print(f"🔄 Starting check cycle at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*50}")
        for username in USERNAMES:
            self.check_user_stories(username)
        self.session_manager.save()
    
    def run_forever(self):
        print("📡 Instagram Story Monitor Started")
        print(f"👥 Monitoring: {', '.join(USERNAMES)}")
        print(f"⏱️  Check interval: {CHECK_INTERVAL} seconds")
        
        try:
            while True:
                self.run_check_cycle()
                print(f"\n💤 Sleeping for {CHECK_INTERVAL} seconds...")
                time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("\n\n🛑 Shutting down gracefully...")
        finally:
            self.session_manager.cleanup()
            print("👋 Goodbye!")


if __name__ == "__main__":
    monitor = StoryMonitor()
    monitor.run_forever()
