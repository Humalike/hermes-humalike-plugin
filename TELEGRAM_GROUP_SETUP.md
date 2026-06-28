# Hermes na Telegramie w grupie — checklista

Wszystko, co trzeba zrobić, żeby bot (turn-taking) działał i odpowiadał ludziom
w grupie. Ustawienia env trafiają do `~/.hermes/.env`; po zmianach **restart gatewaya**.

## 1. Token bota
1. W Telegramie napisz do **@BotFather** → `/newbot` (lub `/token` dla istniejącego).
2. Skopiuj token → `~/.hermes/.env`:
   ```
   TELEGRAM_BOT_TOKEN=<token>
   ```

## 2. Wyłącz privacy mode (żeby bot widział WSZYSTKIE wiadomości grupy)
Domyślnie bot w grupie dostaje tylko wiadomości, w których jest @wspomniany.
Turn-taking musi widzieć wszystko, żeby decydować, kiedy się odezwać.
1. @BotFather → `/setprivacy` → wybierz bota → **Disable**.
2. **Usuń bota z grupy i dodaj ponownie** — zmiana privacy działa dopiero przy nowym członkostwie.

## 3. Autoryzacja — kto dostaje odpowiedzi
To OSOBNA warstwa od turn-taking. Turn-taking decyduje *czy* się odezwać, ale
Hermes i tak odrzuca nieautoryzowanych nadawców (`Unauthorized user` w logach) —
wtedy bot „widzi" wiadomość, ale nigdy nie odpowiada.

- **DM:** każdy użytkownik musi być sparowany. Gdy ktoś napisze do bota, dostaje
  kod; zatwierdzasz go:
  ```
  hermes pairing approve telegram <KOD>
  ```
  (lista: `hermes pairing list`, cofnięcie: `hermes pairing revoke`).

- **Grupa (bez parowania każdej osoby):** autoryzuj **cały czat po ID** — wtedy
  każdy członek grupy działa automatycznie:
  ```
  TELEGRAM_GROUP_ALLOWED_CHATS=<chat_id1>,<chat_id2>   # lista przecinkami; * = wszystkie grupy
  ```
  Chat_id grupy znajdziesz w logach gatewaya: `tt inbound: chat=<chat_id>`
  (grupy Telegrama mają ujemne ID).

  ⚠️ **Każda grupa ma WŁASNY chat_id** — grupa z topikami (forum) to inny czat niż
  zwykła grupa. Dodanie jednej nie autoryzuje drugiej; każdą nową grupę trzeba
  dopisać do listy (przecinkami) i zrestartować gateway. Objaw pominięcia:
  `Unauthorized user` mimo że „inna grupa działa".

  Alternatywy: `TELEGRAM_GROUP_ALLOWED_USERS=<id1>,<id2>` (tylko wymienione osoby),
  albo parowanie każdej osoby z osobna.

## 4. Restart gatewaya
```
cd ~/repos/hermes-agent
.venv/bin/hermes gateway run -v
```
Sprawdź w logach: `✓ telegram connected` oraz `tt inbound / tt decide / tt forward`.

## Aktualna konfiguracja tego setupu
- Bot: `t.me/minimiliani_bot`
- Grupa autoryzowana: `TELEGRAM_GROUP_ALLOWED_CHATS=-1004418899432` (grupa z topikami;
  zastąpiła starą `-5359080915`, którą przekonwertowano na forum)
- DM: sparowany tylko właściciel (`hermes pairing list`)

## Szybka diagnoza „bot nie odpowiada w grupie"
1. **Brak `tt inbound` dla wiadomości** → privacy mode włączony (krok 2) lub bot nie jest w grupie.
2. **Jest `tt decide ... stay_silent`** → turn-taking świadomie milczy (poprawne; odzywa się selektywnie).
3. **`Unauthorized user: <id>` w logach** → nadawca nieautoryzowany (krok 3).
4. **`tt respond ... DROPPED (superseded)`** → szybka seria wiadomości; dostarczana jest tylko najnowsza odpowiedź.
