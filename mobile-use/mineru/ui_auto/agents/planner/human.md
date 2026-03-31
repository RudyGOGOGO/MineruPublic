Here is your input.

---

**Action (plan or replan)**: {{ action }}

**Initial Goal**: {{ initial_goal }}

{% if active_lessons %}
**Prior Knowledge (from previous sessions):**
Use these known navigation paths to plan more efficiently. Prefer known paths over guessing.

{{ active_lessons }}
{% endif %}

{% if action == "replan" %}
Relevant only if action is replan:

**Previous Plan**: {{ previous_plan }}
**Agent Thoughts**: {{ agent_thoughts }}
{% endif %}
