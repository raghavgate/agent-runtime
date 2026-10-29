# Persistent conversations

The browser workspace now keeps a conversation across messages, page refreshes and server restarts. One-shot `run` and evaluation commands remain independent and keep their existing contracts.

## User flow

1. Ask “Find open tickets for Northstar.”
2. When asked to choose an account, reply “Labs.”
3. Ask “Who is their primary contact?” or “What is their open pipeline?”

In live-model mode, the previous user messages, model outputs, tool requests and tool results are carried into each new turn. The account identity and earlier evidence remain available. Each answer has its own readable citations and source cards. The running estimated conversation cost appears above the transcript.

Use **New conversation** to start an independent context. **History** lists resumable conversations. The earlier single-question traces remain available separately; opening one does not silently add it to a conversation.

The scripted demo supports the clarification flow, ticket questions, and simple account-owner/renewal follow-ups. Contact, pipeline and meeting-preparation reasoning require a real model. The demo is still not an LLM or an accuracy benchmark.

Meeting-preparation and renewal-review starters prefill explicit requests using the existing tools. They are configurable prompts, not a separate hardcoded orchestration engine. As before, unavailable systems or mappings should be explained rather than filled with invented facts.

## State boundaries

- `conversations.py` stores a private JSON snapshot per conversation, using owner-only files in a Git-ignored `.conversations/` directory.
- OpenAI state contains the complete Responses input/output history, including function-call IDs, assistant phase values and opaque encrypted reasoning items. Only the browser-facing turns are returned by the public API; provider state is not returned.
- The runtime accepts optional adapter state and prior source references. The execution loop itself still does not know about CRM schemas or provider wire formats.
- An adapter can implement `start_turn(user_message, state)` and `export_state()`. Existing stateless models still work with one-shot runs.
- Model/provider, dataset and configuration bindings prevent resuming a conversation under different workspace settings. An ordinary restart with the same configuration preserves it. If the configuration changes, start a new conversation; older files are retained rather than mixed into the new context.

## Failures and duplicate submissions

A completed turn saves a new provider state. A failed turn is visible and traceable, but leaves the last successful provider state intact. Retrying is explicit; there are no automatic paid chat retries.

Message requests include a UUID. Repeating a committed request with the same UUID and text returns its saved result without calling a model again; reusing that UUID for different text is rejected. An explicit retry of a saved failed turn uses a new request ID. This is not an exactly-once guarantee across a process crash before a response is saved.

A nonblocking file lock prevents two turns from concurrently editing the same conversation, including across local server processes. The current lock implementation uses macOS/Linux `flock`; Windows support is not claimed.

## Limits and cost

Defaults:

- 24 attempted turns per conversation.
- Six tool calls per turn.
- A 20,000 cumulative-token threshold per turn, checked between model responses.
- A 120,000-byte serialized request-context limit, checked before a provider request.
- The existing per-response output-token limit still applies.

Configure these with `conversation_max_turns`, `conversation_max_tool_calls`, `conversation_max_total_tokens`, and `conversation_max_history_bytes`. There is no silent truncation or automatic summarization; start a new conversation when a limit is reached. Token thresholds are not exact billing caps.

Longer conversations resend prior context, so a follow-up may cost more than an isolated question. Merely opening or resuming a saved conversation makes no model requests. Provider retention policy and `store=False` behavior remain as described in the project README.

## Sources

Source IDs from successful prior turns are available for follow-up citations. Unknown source IDs are rejected. Newly retrieved evidence requires a citation; an acknowledgment with no new retrieval is allowed without attaching an irrelevant citation. Membership checks still do not prove semantic grounding of every claim.

The UI shows only the sources cited by each message and can display evidence retrieved earlier in that conversation. Citation anchors are scoped to their message so repeated source numbers do not point to another answer.

## API

- `POST /api/conversations` creates an empty conversation; no model call.
- `GET /api/conversations` lists conversations matching the current workspace and model.
- `GET /api/conversations/{id}` returns its visible turns.
- `POST /api/conversations/{id}/messages` accepts `{ "message": "Labs", "request_id": "a UUID" }` and returns the updated conversation and turn record.

The existing `/api/run`, trace, tool and evaluation endpoints remain available. This is still a local single-user server, not an authenticated shared deployment.

## Verification

Offline tests cover context persistence across application reloads, source continuity, isolated conversations, failure rollback, duplicate requests, concurrent turns, limits, endpoint behavior, and complete provider-history replay using mocked responses. A live check confirmed account clarification and follow-up context with the OpenAI adapter. The meeting-preparation contract is verified offline and still needs its first measured live run.

Reference: [official OpenAI conversation-state documentation](https://developers.openai.com/api/docs/guides/conversation-state).
