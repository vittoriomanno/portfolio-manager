#!/bin/bash
# Install script for GCP VM (Rocky Linux or similar)

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit
fi

REPO_URL=$1
if [ -z "$REPO_URL" ]; then
    echo "Usage: sudo bash install.sh <repository_url>"
    exit 1
fi

echo "Installing dependencies..."
dnf install -y python3.12 git cronie

echo "Cloning repository..."
git clone $REPO_URL /opt/portfolio_manager

echo "Setting up virtualenv..."
cd /opt/portfolio_manager
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

echo "Initializing log files..."
touch decisions.log trades.log app.log .env
chmod 600 .env decisions.log trades.log app.log

echo "Setting up systemd..."
cp portfolio_manager.service /etc/systemd/system/
cp dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable portfolio_manager
systemctl enable dashboard

echo "Installation complete. Please fill in /opt/portfolio_manager/.env and start the services."
