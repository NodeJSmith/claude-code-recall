---
name: ccr-resume
description: "Use when picking up after the previous session in this project ended: a /clear to free up context, a crash or shutdown, or to get a standing summary of a session that's done and waiting on CI or review. Reads the prior session's transcript tail, checks it against the repo, and surfaces any decision it left open. User-invoked only."
user-invocable: true
disable-model-invocation: true
---

# Resume

The previous session in this project ended, and this one starts with none of its context. Recover where it stood: what the user last asked for, what got done, what was still in flight, and any question it was waiting on the user to answer. Then continue the work or give the user a summary, whichever they asked for.

$ARGUMENTS is the user's direction for this pickup ("keep going but skip the test rewrite", "just summarize"). It is not a session selector.

## The trap

Disk state (git, files, PRs) shows what landed. It never shows that the prior session was waiting on the user. A session can finish its work, ask "ship it?", and end. A next session that reads only the repo sees "done" and ships. So read the transcript tail before acting on what's on disk.

A question the prior session left open belongs to the user. It might be an unanswered `AskUserQuestion` or a question asked in prose at the end of the last message. Surface it, don't pick an answer, and don't do the work it gates. $ARGUMENTS says how to proceed, not how to answer that question. If $ARGUMENTS plainly answers it anyway, confirm your reading with the user before acting on it.

Once you've read the tail, check what actually landed (the working tree, commits, the PR) against what the tail says was in flight, and name any mismatch. After a crash, an edit or commit may already be done, or a file may be half-written. Redoing it or skipping it both cause damage.

## Reading the prior session

`ccrecall tail` picks the prior session (the second-newest transcript for this directory, since the newest is this one) and prints:

- its last user instruction
- its last few events
- how it ended: the last assistant message, or a PENDING QUESTION block if it ended on an unanswered `AskUserQuestion`
- the skills it invoked

`--lines N` shows more events. `--full` replaces the event list with the untruncated messages. For the whole conversation, run `ccrecall recent --session <id>`.

The SessionStart context may also name the prior session. After a `/clear` (its `Source:` line says `clear`), that block names exactly the session that was cleared, while `tail` guesses by recency. When it disagrees with `tail`'s pick, trust it: run `ccrecall tail --list` (which marks this session `<- current`) and then `ccrecall tail <id-substring>`. If `tail` finds nothing, the transcript is gone or the directory moved. `ccrecall recent --path <dir>` still has the stored conversation. Don't fill the gap from disk state. Say what you couldn't recover.

Anything the prior session was watching or running may have stopped with it, especially after a crash or shutdown. That covers CI watches, a PR it meant to merge when green, background jobs, and dev servers. Check where each one stands now instead of assuming it finished or is still being watched.

SKILLS INVOKED lists skills the session started, not ones it finished. To continue inside one, invoke it again so its steps are back in context. Don't work from memory of what it says.

## Done looks like

An open decision is re-asked alongside whichever outcome below applies, after the summary or status. For a PENDING QUESTION, use `AskUserQuestion` with the original option labels. Don't do the work it gates until the user answers.

- **A summary**, because the user asked for one or the work is done and waiting on someone else: the user will leave it on their terminal and read it later, so it has to stand on its own. Cover what the session set out to do and what landed: commits, the PR, and whether it merged. Then say what's outstanding, what's waiting on CI or a reviewer, and anything that stopped partway.
- **A directive to continue:** say in a line or two where things stand and what you're picking up, then do it.
- **No directive and nothing open:** give that short status and ask how the user wants to proceed.
