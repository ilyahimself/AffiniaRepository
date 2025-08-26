# 🔒 Security Guide for Affinia

## 🚨 Critical Security Measures

### 1. **API Token Protection**
- **NEVER** commit API tokens to Git
- **NEVER** hardcode tokens in source code
- **ALWAYS** use environment variables
- **NEVER** share tokens publicly

### 2. **Environment Variables Setup**

#### For Local Development:
```bash
# Windows PowerShell
$env:API_TOKEN="your_bot_token_here"
$env:BOT_USERNAME="your_bot_username"
$env:WEBAPP_URL="http://localhost:5000"

# Windows CMD
set API_TOKEN=your_bot_token_here
set BOT_USERNAME=your_bot_username
set WEBAPP_URL=http://localhost:5000
```

#### For Heroku:
```bash
heroku config:set API_TOKEN=your_bot_token_here
heroku config:set BOT_USERNAME=your_bot_username
heroku config:set WEBAPP_URL=https://your-app.herokuapp.com
```

### 3. **Rate Limiting**
- **100 requests per hour** per user
- **Stricter limits** for sensitive operations (donations)
- **Automatic blocking** of excessive requests

### 4. **Logging & Monitoring**
- **Secure logging** without sensitive data exposure
- **File rotation** to prevent disk space issues
- **Error tracking** for security incidents

### 5. **Data Protection**
- **User data encryption** (planned)
- **Secure file storage** (planned)
- **GDPR compliance** (planned)

## 🛡️ Security Checklist

- [ ] API token moved to environment variables
- [ ] No hardcoded secrets in source code
- [ ] Rate limiting implemented
- [ ] Secure logging configured
- [ ] .gitignore updated
- [ ] Environment variables set in production
- [ ] Regular security audits scheduled

## 🚨 Emergency Response

If you suspect a security breach:

1. **Immediately** revoke the bot token at @BotFather
2. **Generate** a new token
3. **Update** environment variables
4. **Review** logs for suspicious activity
5. **Monitor** for unusual behavior

## 📞 Security Contact

For security issues, contact: [Your Contact Info]

---

**Remember: Security is everyone's responsibility!** 🔒
