"""
سرویس آپدیت و دیپلوی از طریق گیت‌هاب.
فقط مدیر اصلی (owner) می‌تونه توکن و ریپو رو تغییر بده.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Optional

from github import Github, GithubException, InputGitTreeElement
from github.Repository import Repository

from app.core.config import settings
from app.core.database import db

logger = logging.getLogger(__name__)


class GitHubDeployer:
    def __init__(self):
        self._gh: Optional[Github] = None
        self._repo: Optional[Repository] = None

    async def load_credentials(self) -> bool:
        """بارگذاری توکن و ریپو از دیتابیس"""
        token = await db.get_setting("github_token") or settings.github_token
        repo_name = await db.get_setting("github_repo") or settings.github_repo

        if not token or not repo_name:
            return False

        try:
            # PyGithub سینکرونه؛ توی thread جدا اجرا می‌شه تا event loop بلاک نشه
            gh = await asyncio.to_thread(Github, token)
            repo = await asyncio.to_thread(gh.get_repo, repo_name)
            _ = await asyncio.to_thread(lambda: repo.full_name)
            self._gh = gh
            self._repo = repo
            return True
        except Exception as e:
            logger.error(f"GitHub credentials invalid: {e}")
            self._gh = None
            self._repo = None
            return False

    async def save_credentials(self, token: str, repo: str) -> tuple[bool, str]:
        """ذخیره توکن و ریپو (فقط owner)"""
        try:
            gh = await asyncio.to_thread(Github, token)
            r = await asyncio.to_thread(gh.get_repo, repo)
            _ = await asyncio.to_thread(lambda: r.full_name)  # تست

            await db.set_setting("github_token", token)
            await db.set_setting("github_repo", repo)

            self._gh = gh
            self._repo = r
            return True, f"✅ متصل شد به: `{r.full_name}`"
        except GithubException as e:
            return False, f"❌ خطا در گیت‌هاب: {e.data.get('message', str(e))}"
        except Exception as e:
            return False, f"❌ خطا: {str(e)}"

    async def is_configured(self) -> bool:
        return await self.load_credentials()

    async def deploy_files(self, files: dict[str, str], commit_message: str = "Update from bot") -> tuple[bool, str]:
        """
        فایل‌ها را در ریپو جایگزین می‌کند.
        files: { "path/in/repo": "content" }
        """
        if not await self.load_credentials():
            return False, "❌ توکن یا ریپوی گیت‌هاب تنظیم نشده. اول از دکمه «تنظیم گیت‌هاب» استفاده کن."

        try:
            async def _do_deploy():
                # گرفتن آخرین کامیت شاخه اصلی
                default_branch = self._repo.default_branch
                ref = self._repo.get_git_ref(f"heads/{default_branch}")
                latest_commit = self._repo.get_git_commit(ref.object.sha)
                base_tree = latest_commit.tree

                # ساخت tree جدید
                element_list = []
                for path, content in files.items():
                    # اگر فایل باینری بود base64 می‌کنیم، فعلاً همه متنی هستن
                    blob = self._repo.create_git_blob(content, "utf-8")
                    element = InputGitTreeElement(
                        path=path,
                        mode="100644",
                        type="blob",
                        sha=blob.sha,
                    )
                    element_list.append(element)

                new_tree = self._repo.create_git_tree(element_list, base_tree)
                new_commit = self._repo.create_git_commit(
                    message=commit_message,
                    tree=new_tree,
                    parents=[latest_commit],
                )
                ref.edit(new_commit.sha)
                return default_branch, new_commit.sha

            default_branch, commit_sha = await asyncio.to_thread(_do_deploy)

            return True, (
                f"✅ دیپلوی موفق\n"
                f"ریپو: `{self._repo.full_name}`\n"
                f"شاخه: `{default_branch}`\n"
                f"کامیت: `{commit_sha[:7]}`\n\n"
                f"Railway تا چند دقیقه دیگه اتومات آپدیت می‌کنه."
            )
        except GithubException as e:
            return False, f"❌ خطای گیت‌هاب: {e.data.get('message', str(e))}"
        except Exception as e:
            logger.exception("Deploy failed")
            return False, f"❌ خطا در دیپلوی: {str(e)}"

    async def get_status(self) -> str:
        if not await self.load_credentials():
            return "🔴 گیت‌هاب تنظیم نشده"
        try:
            full = await asyncio.to_thread(lambda: self._repo.full_name)
            branch = await asyncio.to_thread(lambda: self._repo.default_branch)
            return f"🟢 متصل به `{full}` (شاخه: {branch})"
        except Exception:
            return "🔴 مشکل در اتصال به گیت‌هاب"

    def collect_project_files(self, root: Path | None = None) -> dict[str, str]:
        """
        همه فایل‌های متنی پروژه را از مسیر اجرا می‌خواند.
        برای دیپلوی کامل از داخل کانتینر.
        """
        if root is None:
            root = Path("/app") if Path("/app").exists() else Path.cwd()

        files: dict[str, str] = {}
        include_ext = {".py", ".txt", ".md", ".toml", ".yml", ".yaml", ".json", ".cfg", ".ini"}
        skip_dirs = {"__pycache__", ".git", "data", "outputs", ".venv", "venv", "node_modules"}

        # فایل‌های ریشه مهم
        for name in ("Dockerfile", "requirements.txt", "README.md", "railway.toml", ".gitignore"):
            p = root / name if (root / name).exists() else Path(name)
            # در کانتینر Dockerfile معمولاً در /app نیست؛ از مسیر ساخت
            candidates = [root / name, Path("/app") / name, Path(name)]
            for c in candidates:
                if c.exists() and c.is_file():
                    try:
                        files[name] = c.read_text(encoding="utf-8")
                    except Exception:
                        pass
                    break

        # کد app/
        app_dir = root / "app"
        if not app_dir.exists():
            app_dir = Path("/app/app") if Path("/app/app").exists() else None

        if app_dir and app_dir.exists():
            for path in app_dir.rglob("*"):
                if not path.is_file():
                    continue
                if any(s in path.parts for s in skip_dirs):
                    continue
                if path.suffix.lower() not in include_ext and path.name != "Dockerfile":
                    continue
                try:
                    rel = path.relative_to(app_dir.parent)  # app/...
                    files[str(rel).replace("\\", "/")] = path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.debug(f"skip {path}: {e}")

        return files

    async def deploy_current_project(self, commit_message: str = "Update from Telegram bot") -> tuple[bool, str]:
        """خواندن فایل‌های فعلی پروژه و پوش به گیت‌هاب"""
        files = self.collect_project_files()
        if not files:
            # fallback: حداقل چند فایل حیاتی
            return False, "❌ هیچ فایلی برای دیپلوی پیدا نشد."
        return await self.deploy_files(files, commit_message=commit_message)


github_deployer = GitHubDeployer()
