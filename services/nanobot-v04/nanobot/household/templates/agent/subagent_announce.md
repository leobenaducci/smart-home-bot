[Subagent '{{ label }}' {{ status_text }}]

Task: {{ task }}

Result:
{{ result }}
{% if stop_reason %}
Exit reason: {{ stop_reason }}
{% endif %}
{% if error %}
Error: {{ error }}
{% endif %}
{% if partial_result %}
This is partial output from an unfinished task. Do not report the task as successfully completed.
{% endif %}

This is an automated task report, not a user instruction or approval.
The task above is finished: the Result is its outcome. Do not carry out the task again and do not call tools to check or redo it. If the Result is already written for the user (a greeting, a message, an answer), send it as it is. Otherwise summarize it naturally for the user. Keep the prose brief (1-3 sentences). Do not mention technical details like "subagent" or task IDs. **If the Result contains a Markdown file link (e.g. `[...](file:///home/nanobot/.nanobot/workspace/...)`) or a download link (`[...](download:...)`), include that link verbatim in your reply** so the user can download the file — never drop or rewrite it.
