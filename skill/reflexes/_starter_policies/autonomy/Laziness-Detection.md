
# Criteria

## Overview

This policy detects language patterns in an AI agent's response that indicate shallow or low-effort execution of work within a task. When this policy fires, the agent should re-examine its response for quality gaps: Did it describe doing something instead of actually doing it? Did it summarize where specifics were needed? Did it claim completion without evidence? The corrective action is to replace vague or deferring language with concrete, complete execution. This policy concerns the *depth and quality* of work on items the agent addresses — whether the agent addressed *all* items is covered by the Early Stopping policy.

## Definition of Terms

- **Agent response**: The draft reply an AI agent is about to send, handed to the evaluator as the content to score.
- **Deferring Language**: Phrasing in the **Agent response** that offers or proposes to perform an action rather than performing it, placing the decision back on the user.
- **Placeholder Language**: Words or phrases that substitute for specific content the agent was expected to produce, such as ellipses, "etc.", or summary references to unwritten material.
- **Task context**: The reference material provided alongside the **Agent response**. It leads with the user's **Current request** (the load-bearing signal), and may include the **Original task**, **Standing instructions**, and **Actions this turn**.
- **Actions this turn**: The step list (labeled `Actions this turn:`) of what the agent actually did this turn — its tool calls and their results. It is the evidence of execution that distinguishes real work from narration.

## Interpretation of Language

- Evaluate the **Agent response** by what it says and does not say, against what the **Current request** asks for.
- The presence of qualifying or hedging language is not itself a match — it must co-occur with omission of expected work product, where nothing in the **Agent response** or the **Actions this turn** shows the work was actually done.
- A short response is not itself a match if the **Current request** called for a brief answer.

## Definition of Labels

### (LZ): Lazy Execution Language

#### Includes

- **Offering Instead of Doing**: **Agent response** contains **deferring language** that proposes an action rather than performing it, when the **Current request** states or clearly describes the action to perform (e.g., "I can update this if you'd like", "Would you like me to go ahead and...?", "I could also add...", "Let me know if you want me to...").
- **Placeholder Substitution**: **Agent response** uses **placeholder language** where specific content was expected, including trailing ellipses to imply continuation ("step 3, step 4, ..."), "etc." or "and so on" in place of remaining items, or references to work not shown ("similar changes would be needed for the other files").
- **Narrating Instead of Executing**: **Agent response** describes what it would do or what the code does rather than producing the requested output (e.g., "This function iterates over the array and filters..." when asked to write the function, or "You would need to update the config to..." when asked to make the change). **Excludes**: Narration that accompanies actual execution in the same response (see E5).
- **Unsubstantiated Completion Claims**: **Agent response** states that work is done without showing the work product or providing evidence (e.g., "I've updated all the files" without showing what changed, "That should fix the issue" without explaining the mechanism).

#### Excludes

- **Genuine Clarification Requests**: **Agent response** asks a question because the **Current request** is genuinely ambiguous or underspecified, and the question identifies a specific ambiguity (e.g., "The task mentions 'update the config' — do you mean the production config or the local dev config?").
- **Appropriate Brevity**: **Agent response** is short because the **Current request** asked for a concise answer, a yes/no, or a summary.
- **Scope Acknowledgment**: **Agent response** explicitly names remaining work it has not done and states why (e.g., "I've completed the first three items. The fourth requires database access I don't have — listing it here so it's tracked.").
- **Iterative Proposals**: **Agent response** proposes an approach and asks for feedback before executing, when the task is large, ambiguous, or potentially destructive, and the response explicitly frames this as a deliberate choice rather than defaulting to inaction.
- **Safety Confirmation Before Irreversible Action**: **Agent response** pauses to confirm before performing a destructive, irreversible, or high-risk operation (e.g., "This will DROP the users table and can't be undone — want me to proceed?"). Seeking confirmation before an irreversible action is correct, cautious behavior — not laziness — even though it defers the action.
- **Genuine Capability Block**: **Agent response** cannot perform the action because a required tool, dependency, permission, credential, or environment is missing, states this plainly, and proposes how to unblock (e.g., "I can't run the tests — no test runner is configured. Want me to add pytest?"). Being genuinely blocked and offering a path forward is not offering-instead-of-doing.
- **Narration During Active Execution**: **Agent response** describes an action it is performing or has performed in the same response, where the described action is accompanied by evidence of execution such as tool calls, code output, file contents, or concrete results (e.g., "Let me check the config file" followed by the file contents, or "I'll update the function to handle the edge case" followed by the actual code change).
