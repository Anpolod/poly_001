@echo off
cd /d D:\dev\projects\polymarket-sports
set PYTHONUTF8=1
C:\Users\siriu\AppData\Local\Programs\Python\Python312\python.exe -m analytics.prop_scanner --daemon >> logs\scanner.log 2>&1
