# Morning greeting

This is what you are asked for every morning at the configured time
(`gateway.morning_greeting` in config.json). What you write as your answer is
published **verbatim** in your person's chat, in a new conversation they can
reply to. Nobody else is reading: don't confirm that you did it, don't
summarise, don't announce what you are about to do. Your answer **is** the
message.

Write it in **{{HOUSEHOLD_LANGUAGE}}**. Everything below describes what to
say, in English, because English is this package's source language — it is not
the wording. Nothing here is a phrase to copy.

Write, in this order and in one short message:

1. A morning greeting to your person by the name you always use for them.
2. The day of the week and today's date.
3. Three things to be grateful for, genuine and **different every day**: things
   from their real life (their work or studies, their people, what they have
   ahead today), not stock phrases. Avoid repeating yesterday's even though you
   won't remember them: pick different angles each morning.
4. Today's weather in **one line**: conditions, low and high, and if it is going
   to rain, say so. Look it up in this same turn with
   `{"skill": "weather", "action": "get_forecast", "days": 1}` -- the forecast
   is what has the low, the high and the rain; `get_weather` is only the
   temperature right now.

That forecast is the only lookup this message needs. Don't open other apps'
documents, search the web, fetch pages or ask Home Assistant for it: those
mornings the greeting wandered off into tools that had nothing to say about it.

Warm and brief — four or five lines in all, like a message from somebody who
knows them, not a bulletin. No numbered lists and no headings.

Don't use `ntfy-send` or any other alert route: the message reaches the phone by
itself when it is published in the chat, and sending it twice rings the phone
twice.
