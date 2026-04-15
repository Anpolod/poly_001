#!/bin/bash
PROJECT=/Volumes/SanDisk/dev/projects/polymarket-sports
LOG=$PROJECT/logs/git_sync.log

cd $PROJECT || exit 1
git fetch origin >> $LOG 2>&1

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') New commits — pulling..." >> $LOG
    git reset --hard origin/master >> $LOG 2>&1
    echo "$(date '+%Y-%m-%d %H:%M:%S') Done: $(git log -1 --format='%h %s')" >> $LOG
    touch $PROJECT/dashboard/main_dashboard.py
fi
