# İSU Haber Takip

Kocaeli yerel haber sitelerini 5 dakikada bir tarar; `anahtar_kelimeler.txt` içindeki
kelimelerden biri geçen yeni haberleri telefona bildirim olarak gönderir.

- Siteler: `siteler.txt` (her satıra bir site)
- Anahtar kelimeler: `anahtar_kelimeler.txt` (her satıra bir kelime)
- Bildirim: ntfy uygulaması, konu adı GitHub gizli ayarı `NTFY_TOPIC` içinde
  (isteğe bağlı: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, `WHATSAPP_PHONE` + `WHATSAPP_APIKEY`)

Liste değiştirmek için GitHub'da ilgili .txt dosyasını açıp kalem simgesiyle düzenleyin ve kaydedin.
Elle çalıştırmak için: Actions > ISU Haber Takip > Run workflow.
