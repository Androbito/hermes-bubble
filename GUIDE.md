# Hermes Bubble — what you can say

**How to use it:** hold the **🌐 Globe key** for about half a second. You'll hear a beep and see the Hermes bubble. Talk, then pause. Hermes answers out loud in the language you spoke (any of the `languages` in your config).
- **Keep talking:** after an answer the bubble stays open and listens for a follow-up. Hold 🌐 again anytime to talk.
- **Close:** click **✕** on the bubble to end the conversation.
- **Interrupt:** hold 🌐 while Hermes is talking to stop it.
- **Continue in Terminal:** click the **Hermes notification** that comes with each answer. It opens the same conversation in Terminal, where you can type.

Commands marked ⚡ run instantly without asking the AI. The others go through your bubble model and take a few seconds (a small fast model such as Claude Haiku or GPT-mini feels best).

---

## 🎵 YouTube (in your browser)

- **Pause** ⚡ — "Pause" · "Mets pause" · "Arrête la musique" · "Stop the music"
- **Resume** ⚡ — "Reprends" · "Play" · "Resume"
- **Next video** ⚡ — "Suivant" · "Chanson suivante" · "Next song" · "Skip". Uses YouTube's Next button, or the first recommended video when there isn't one.
- **Previous video** ⚡ — "Précédent" · "Previous song"
- **What's playing** ⚡ — "Qu'est-ce qui joue ?" · "What's playing?"
- **Search YouTube and play** ⚡ — "Cherche sur YouTube Stromae" · "Mets-moi Fairuz sur YouTube" · "Play Bohemian Rhapsody on YouTube". Opens the first real result in your browser, skipping ads and Shorts.
- **Replay from your history** — "Retrouve dans mon historique la vidéo de Daft Punk" · "Play the lofi video I watched". Searches the YouTube videos in your Chrome history.

## 🎧 Spotify

- **Search and play** ⚡ — "Mets Linkin Park Numb sur Spotify" · "Cherche sur Spotify Stromae" · "Play jazz piano on Spotify". Plays the top song, album, playlist or artist.
- **Your library on shuffle** ⚡ — "Ouvre ma bibliothèque et joue au hasard" · "Mets mes titres likés en aléatoire" · "Play my library on shuffle"
- **Pause / resume / next / previous** ⚡ — the same commands as for YouTube: "Pause" · "Reprends" · "Suivant" · "Précédent". They control **whatever is playing right now**. To force a player, add its name: "Pause sur Spotify" · "Suivant sur YouTube". If nothing is playing, "Reprends" resumes the player you used last.
- **What's playing** ⚡ — "Qu'est-ce qui joue ?" names the Spotify track and artist.
- **One player at a time:** starting Spotify pauses YouTube, and starting a YouTube video pauses Spotify.

## 🔊 Volume

- **Louder / quieter** ⚡ — "Monte le son" · "Baisse le volume" · "Louder" · "Turn it down"
- **Set a level** ⚡ — "Volume à 40" · "Set volume to 60"
- **Mute / unmute** ⚡ — "Coupe le son" · "Remets le son" · "Mute"

## 🖥️ Apps & documents

- **Open an app** ⚡ — "Ouvre Calculatrice" · "Launch Xcode" · "Open WhatsApp". Any app installed on the Mac; French names like Calculatrice, Plans, Rappels and Réglages work too.
- **New BBEdit document** ⚡ — "Ouvre un nouveau document BBEdit" · "New BBEdit document"

## 🌐 Web (Chrome)

- **Google search, results shown in Chrome** ⚡ — "Cherche la météo à Casablanca sur Google" · "Search Google for PS5 prices"
- **Open a website** — "Ouvre le site lemonde.fr" · "Open github.com"
- **Answer from the internet** (Hermes searches and tells you) — "Quel temps fera-t-il demain ?" · "Who won the match last night?"

## 📞 WhatsApp

- **Voice call** ⚡ — "Appelle Sarah" · "Appelle Karim sur WhatsApp" · "Call Alex"
- **Video call** ⚡ — "Appelle Sarah en vidéo" · "Call Alex on video"
- Hermes finds the person in your WhatsApp chats, or searches for them, then starts the call. If no name matches closely enough, it tells you instead of guessing.

## ⏱️ Timers

- **Countdown with a sound and a spoken alert** — "Mets un minuteur de 10 minutes pour les pâtes" · "Set a 5 minute timer"

## 👀 Screen & questions

- **Read what's on screen** — "Qu'est-ce qu'il y a sur mon écran ?" · "What's in this WhatsApp message?"
- **General questions** — anything: facts, translations, calculations, advice. The answer is short and spoken.

---

## Limits

- **No free clicking or typing.** Hermes only performs the actions listed above. Anything else that would change your Mac (delete, send messages, install…) has to be done from your normal Hermes chat or Terminal.
- **YouTube controls need the video tab to be the active tab** of a browser window (the bubble reads that window's player buttons).
- **HermesKey must be running.** It starts automatically at login (`hermes bubble status` to check).
- **Timers are lost** if the voice worker restarts. It restarts on its own every 6 hours, only while idle.
- **Calls start right away.** Say the name clearly; if no contact matches closely, nothing is dialled.

## Roadmap (ideas)

- 📝 **Notes & reminders:** "Rappelle-moi d'appeler Sarah à 18 h" (Apple Reminders), "Note : acheter du lait" (Apple Notes)
- 💬 **WhatsApp message:** "Envoie à Karim : j'arrive dans 10 minutes" (with a spoken confirmation before sending)
- 📅 **Calendar:** "Qu'est-ce que j'ai demain ?"
- 📧 **Email summary:** "Résume mes nouveaux mails"
- 🌙 **Mac controls:** lock screen, Do Not Disturb, brightness, screenshot
