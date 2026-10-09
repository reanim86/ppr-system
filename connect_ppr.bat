@echo off
chcp 65001 >nul
rem Подключение сетевого диска N: к общему ресурсу PPR на сервере 192.168.0.206.
rem Запускайте после перезагрузки ПК (или при необходимости).
rem Доступ: логин agentai / пароль от Samba (тот же, что и для SSH).

net use N: \\192.168.0.206\ppr /user:agentai Y8O69eai458J6qZNqDuD 2>nul
if errorlevel 1 (
  echo Ошибка подключения. Проверьте, что сервер доступен и введите пароль вручную:
  net use N: \\192.168.0.206\ppr /user:agentai
) else (
  echo Диск N: подключен.
  echo Файл базы данных для DBeaver: N:\ppr.db
)
pause