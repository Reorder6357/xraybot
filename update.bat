@echo off
chcp 65001 >nul
title Xraybot Updater

rem ============================================
rem  ابزار آپدیت ربات xraybot (ویندوز)
rem  طرز استفاده:
rem     update.bat xraybot_fixes_batch2.patch
rem  فایل patch می‌تونه هر جایی باشه (مسیر کامل بدید)
rem ============================================

if "%1"=="" (
    echo.
    echo  استفاده:  update.bat  file.patch
    echo  مثال:     update.bat  xraybot_fixes_batch2.patch
    echo.
    exit /b 1
)

if not exist "%1" (
    echo.
    echo  فایل patch پیدا نشد: %1
    echo  مسیر درست رو بدید.
    echo.
    exit /b 1
)

echo.
echo  [1/5] بررسی سازگاری patch...
git apply --check "%1"
if errorlevel 1 (
    echo.
    echo  ⚠️ patch قابل اعمال نیست.
    echo  احتمالا قبلا اعمال شده، یا فایل‌های ریپوت با نسخه گیت‌هاب فرق دارن.
    echo  اول این دستور رو بزن:  git pull --rebase origin main
    echo  بعد دوباره امتحان کن.
    echo.
    exit /b 1
)

echo  [2/5] اعمال تغییرات...
git apply "%1"

echo  [3/5] جلوگیری از commit شدن فایل‌های ابزار...
findstr /C:"*.patch" .gitignore >nul 2>&1 || echo *.patch>> .gitignore
findstr /C:"update.bat" .gitignore >nul 2>&1 || echo update.bat>> .gitignore
findstr /C:"update.sh" .gitignore >nul 2>&1 || echo update.sh>> .gitignore

echo  [4/5] ثبت commit...
git add -A
git diff --cached --quiet
if errorlevel 1 (
    git commit -m "apply fixes: %1"
) else (
    echo  چیزی برای commit وجود نداره - ادامه...
)

echo  [5/5] ارسال به گیت‌هاب...
git push origin main
if errorlevel 1 (
    echo.
    echo  ❌ push انجام نشد. (مشکل دسترسی - توضیح توی راهنما)
    exit /b 1
)

echo.
echo  ✅ تمام شد! Railway تا چند دقیقه دیگه خودش آپدیت می‌کنه.
echo     بعدش می‌تونی فایل %1 رو پاک کنی.
echo.
pause
