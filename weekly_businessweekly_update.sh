#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/harrison/Desktop/tickets/ticket finding"
ENV_FILE="$PROJECT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
  source "$ENV_FILE"
fi

cd "$PROJECT_DIR"

/Users/harrison/.pyenv/versions/3.11.8/bin/python3 scrape_businessweekly_concerts.py \
  --output businessweekly_concerts.json \
  --diff-output businessweekly_concerts_changes.json \
  --log businessweekly_concerts.log
