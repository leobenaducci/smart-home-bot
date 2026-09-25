[Subagent '{{ label }}' {{ status_text }}]

Task: {{ task }}

Result:
{{ result }}

Summarize this naturally for the user. Keep the prose brief (1-3 sentences). Do not mention technical details like "subagent" or task IDs. **If the Result contains a Markdown file link (e.g. `[...](file:///home/nanobot/.nanobot/workspace/...)`), include that link verbatim in your reply** so the user can download the file — never drop or rewrite it.
