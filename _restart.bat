@echo off
taskkill /F /PID 15180 >nul 2>&1
taskkill /F /PID 20248 >nul 2>&1
timeout /t 2 >nul
start "Crypto Screener" cmd /k "C:\Users\lanco\AppData\Local\Programs\Python\Python313\python.exe main.py"
