# Affinia - Relationship Management Mini App

## Overview
Affinia is a Telegram Mini App that revolutionizes relationship management through blockchain technology. Users can verify consent, manage intimate partnerships, and earn AFN tokens for meaningful connections.

## Features
- **Consent Verification System** - Secure consent management with blockchain verification
- **Partner Connection Management** - Manage intimate partnerships and trusted relationships
- **AFN Token Rewards** - Earn tokens for meaningful connections and activities
- **TON Blockchain Integration** - Secure wallet connections via TON Connect
- **Telegram Analytics** - User behavior tracking and insights

## Technical Stack
- **Frontend**: HTML5, CSS3, JavaScript
- **Backend**: Python Flask
- **Blockchain**: TON Connect SDK
- **Analytics**: Telegram Mini Apps Analytics SDK
- **Deployment**: Heroku
- **Database**: JSON file storage (ephemeral)

## Architecture
- **Telegram Bot**: Handles user interactions and Mini App launching
- **Mini App**: Web-based interface for relationship management
- **TON Integration**: Wallet connections and blockchain operations
- **Analytics**: User behavior tracking and app performance metrics

## Current Status
- **Stage**: MVP
- **Network**: Testnet
- **Status**: Ready for Telegram Apps Center submission
- **Features**: Core functionality implemented, TON Connect integrated

## Installation
1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Set environment variables (see config.example.py)
4. Run: `python app.py`

## Environment Variables
- `API_TOKEN`: Telegram bot token
- `BOT_USERNAME`: Bot username
- `WEBAPP_URL`: Mini App URL
- `USE_WEBHOOK`: Webhook mode (True/False)

## Team
Solo developer with experience in web development and blockchain integration.

## Links
- **Telegram Bot**: @affinia_bot
- **GitHub**: https://github.com/ilyahimself/AffiniaRepository
- **Live App**: https://affinia.herokuapp.com