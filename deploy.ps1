# Автоматический деплой в Heroku и GitHub
param(
    [string]$message = "Auto deploy $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
)

Write-Host "🚀 Начинаю автоматический деплой..." -ForegroundColor Green

# Git add и commit
git add -A
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Ошибка git add" -ForegroundColor Red
    exit 1
}

git commit -m $message
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Ошибка git commit (возможно, нет изменений)" -ForegroundColor Yellow
}

# Push в Heroku
Write-Host "📦 Деплою в Heroku..." -ForegroundColor Cyan
git push heroku master
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Ошибка деплоя в Heroku" -ForegroundColor Red
    exit 1
}

# Push в GitHub
Write-Host "📤 Отправляю в GitHub..." -ForegroundColor Cyan
git push origin master
if ($LASTEXITCODE -ne 0) {
    Write-Host "⚠️ Ошибка отправки в GitHub (может потребоваться ручное разрешение конфликтов)" -ForegroundColor Yellow
} else {
    Write-Host "✅ Успешно отправлено в GitHub!" -ForegroundColor Green
}

Write-Host "🎉 Деплой завершен!" -ForegroundColor Green
Write-Host "🌐 Приложение: https://affinia-f56170a22b09.herokuapp.com/" -ForegroundColor Blue

