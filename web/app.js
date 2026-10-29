const $ = id => document.getElementById(id);
let evaluationReady = false;
let evaluationCounts = null;
let conversationSupported = false;
let conversationId = null;
let conversationStorageKey = null;
let currentConversation = null;
let chatBusy = false;
let pendingMessage = null;

function money(value) {
  if (value === null || value === undefined) return 'Estimate unavailable';
  return value > 0 && value < 0.0001 ? '< $0.0001' : '$' + value.toFixed(4);
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || 'Request failed');
    error.run = payload.run;
    error.conversation = payload.conversation;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function notice(text, error = false) {
  $('notice').textContent = text;
  $('notice').className = error ? 'error' : '';
}

function showView(name) {
  document.querySelectorAll('.view').forEach(view => {
    view.hidden = view.id !== `view-${name}`;
  });
  document.querySelectorAll('[data-view]').forEach(button => {
    if (button.dataset.view === name) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
}

function sourceLabel(source) {
  if (source.id.startsWith('support:organizations/')) return 'Open ticket search';
  return source.label;
}

function systemLabel(system) {
  return {crm: 'CRM', support: 'Support'}[system] || 'Connected data';
}

function readableDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('en-US', {
    month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC',
  }).format(date);
}

// Format the supported answer elements with DOM nodes, never HTML from a model.
// Source identifiers remain in the stored answer; only readable references are rendered.
function appendInline(parent, text, sources, options = {}) {
  const tokens = /\[([a-zA-Z][\w-]*:[^\]\s]+)\]|\*\*([^*]+)\*\*|`([^`]+)`/g;
  let cursor = 0;
  for (const match of text.matchAll(tokens)) {
    parent.append(document.createTextNode(text.slice(cursor, match.index).replace(/\b\d{4}-\d{2}-\d{2}\b/g, readableDate)));
    if (match[1]) {
      const index = sources.findIndex(source => source.id === match[1]);
      if (index >= 0) {
        const reference = node('a', String(index + 1), 'citation');
        reference.href = `#${options.prefix || ""}source-${index + 1}`;
        reference.title = sourceLabel(sources[index]);
        reference.setAttribute('aria-label', `View source ${index + 1}: ${sourceLabel(sources[index])}`);
        reference.addEventListener('click', () => { (options.disclosure || $('source-disclosure')).open = true; });
        parent.append(reference);
      } else {
        parent.append(document.createTextNode('(unverified reference)'));
      }
    } else if (match[2]) {
      appendInline(parent.appendChild(node('strong')), match[2], sources, options);
    } else {
      parent.append(document.createTextNode(match[3]));
    }
    cursor = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(cursor).replace(/\b\d{4}-\d{2}-\d{2}\b/g, readableDate)));
}

function renderAnswer(text, sources, target = $('answer'), options = {}) {
  const blocks = [];
  let list = null;
  for (const line of text.split('\n')) {
    if (!line.trim()) { list = null; continue; }
    const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)/);
    if (bullet) {
      if (!list) { list = node('ul'); blocks.push(list); }
      appendInline(list.appendChild(node('li')), bullet[1], sources, options);
    } else {
      list = null;
      const heading = line.match(/^#{1,4}\s+(.+)/);
      const block = node(heading ? 'h3' : 'p');
      appendInline(block, heading ? heading[1] : line, sources, options);
      blocks.push(block);
    }
  }
  target.replaceChildren(...blocks);
}

function sourceFacts(source, run) {
  const records = (run.evidence_events || run.events).flatMap(event => (
    event.type === 'tool_result' && Array.isArray(event.result?.data) ? event.result.data : []
  ));
  const collection = [
    ['crm:accounts/', 'customer_id'], ['crm:contacts/', 'contact_id'],
    ['crm:deals/', 'deal_id'], ['support:tickets/', 'ticket_id'],
  ].find(([prefix]) => source.id.startsWith(prefix));
  if (!collection) return [];
  const [prefix, idField] = collection;
  const recordId = source.id.slice(prefix.length);
  const record = records.reverse().find(row => String(row[idField]) === recordId);
  if (!record) return [];
  const fields = {
    owner: 'Owner', renewal_date: 'Renewal date', annual_value: 'Annual value',
    full_name: 'Contact', role: 'Role', email: 'Email', is_primary: 'Primary contact',
    last_contacted_date: 'Last contacted', amount: 'Amount', stage: 'Stage',
    expected_close_date: 'Expected close',
    status: 'Status', priority: 'Priority', subject: 'Subject',
  };
  return Object.entries(fields).filter(([key]) => record[key] !== undefined).map(([key, label]) => {
    let value = ['annual_value', 'amount'].includes(key) ? new Intl.NumberFormat('en-US', {
      style: 'currency', currency: 'USD', maximumFractionDigits: 0,
    }).format(record[key]) : String(record[key]);
    if (record[key] === null) value = 'Not recorded';
    else if (typeof record[key] === 'boolean') value = record[key] ? 'Yes' : 'No';
    else if (['renewal_date', 'last_contacted_date', 'expected_close_date'].includes(key)) value = readableDate(value);
    else if (['status', 'priority', 'stage'].includes(key)) value = value.charAt(0).toUpperCase() + value.slice(1).toLowerCase().replaceAll('_', ' ');
    return [label, value];
  });
}

function renderSources(run, options = {}) {
  const disclosure = options.disclosure || $('source-disclosure');
  const summary = options.summary || $('source-summary');
  const target = options.target || $('sources');
  summary.textContent = `${run.sources.length} ${run.sources.length === 1 ? 'source' : 'sources'} consulted`;
  disclosure.hidden = !run.sources.length;
  disclosure.open = false;
  target.replaceChildren(...run.sources.map((source, index) => {
    const card = node('article', undefined, 'source-card');
    card.id = `${options.prefix || ""}source-${index + 1}`;
    card.tabIndex = -1;
    const heading = node('div', undefined, 'source-heading');
    heading.append(node('span', String(index + 1), 'source-number'), node('h3', sourceLabel(source)), node('span', systemLabel(source.system), 'source-system'));
    card.append(heading);
    const facts = sourceFacts(source, run);
    if (facts.length) {
      const definition = node('dl', undefined, 'source-facts');
      facts.forEach(([label, value]) => definition.append(node('dt', label), node('dd', value)));
      card.append(definition);
    }
    return card;
  }));
}

function friendlyError() {
  return 'We couldn’t complete this question. Please try again or check the workspace connection.';
}

function showRun(run) {
  if (chatBusy) { notice('Please wait for the current answer.'); return; }
  rememberConversation(null);
  currentConversation = null;
  pendingMessage = null;
  $('chat-thread').hidden = true;
  $('workspace-intro').hidden = false;
  $('examples').hidden = false;
  $('workflow-shortcuts').hidden = false;
  $('conversation-caption').textContent = 'Saved single answer · New messages start a separate conversation';
  $('result').hidden = false;
  $('run-status').textContent = run.status === 'completed' ? 'Ready' : 'Needs attention';
  $('run-status').className = `answer-status ${run.status === 'failed' ? 'failed' : ''}`;
  renderAnswer(run.answer || friendlyError(), run.sources);
  renderSources(run);
  $('answer-cost').textContent = run.estimated_cost_usd == null ? '' : `Est. ${money(run.estimated_cost_usd)}`;
  showDiagnostics(run);
}

function showDiagnostics(run) {
  // IDs, timings, raw records and schemas stay in the developer disclosure.
  $('metrics').replaceChildren(node('p', `Run ${run.run_id}`), node('p', `${run.tool_calls} tool calls · ${run.duration_ms} ms`));
  if (run.usage_available) $('metrics').append(node('p', `${run.usage.input_tokens} input / ${run.usage.output_tokens} output tokens`));
  if (run.estimated_cost_usd !== null) $('metrics').append(node('p', `Estimated cost: $${run.estimated_cost_usd.toFixed(6)}`));
  $('events').replaceChildren(...run.events.map(event => {
    const details = node('details');
    details.append(node('summary', event.type + (event.tool_name ? ` · ${event.tool_name}` : '')), node('pre', JSON.stringify(event, null, 2)));
    return details;
  }));
}

async function history() {
  const runs = await api('/api/runs');
  $('history').replaceChildren(...runs.slice(0, 12).map(run => {
    const button = node('button', undefined, 'history-item');
    const text = node('span', undefined, 'history-question');
    text.append(node('strong', run.user_message), node('small', new Date(run.started_at).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'})));
    button.append(text, node('span', run.status === 'completed' ? 'View answer →' : 'Needs attention', 'history-action'));
    button.addEventListener('click', async () => {
      try {
        showRun(await api(`/api/runs/${run.run_id}`));
        showView('workspace');
        $('message').value = run.user_message;
        notice('');
        $('result').scrollIntoView({behavior: 'smooth', block: 'start'});
      } catch { notice('This answer could not be loaded. Please try again.', true); }
    });
    return button;
  }));
  if (!runs.length) $('history').append(node('p', 'Your questions will appear here.', 'empty-state'));
}

function rememberConversation(id) {
  conversationId = id;
  try {
    if (conversationStorageKey) {
      if (id) localStorage.setItem(conversationStorageKey, id);
      else localStorage.removeItem(conversationStorageKey);
    }
  } catch { /* The chat remains usable when browser storage is unavailable. */ }
}

function newConversation() {
  if (chatBusy) return;
  rememberConversation(null);
  currentConversation = null;
  pendingMessage = null;
  $('chat-thread').hidden = true;
  $('result').hidden = true;
  $('chat-messages').replaceChildren();
  $('workspace-intro').hidden = false;
  $('examples').hidden = false;
  $('workflow-shortcuts').hidden = false;
  $('conversation-caption').textContent = 'New conversation';
  $('message').value = '';
  $('message').placeholder = 'What would you like to know?';
  $('run-button').textContent = 'Send message ↗';
  notice('');
  showView('workspace');
  $('message').focus();
}

function renderConversation(conversation) {
  currentConversation = conversation;
  rememberConversation(conversation.conversation_id);
  $('result').hidden = true;
  const hasTurns = conversation.turns.length > 0;
  $('chat-thread').hidden = !hasTurns;
  $('workspace-intro').hidden = hasTurns;
  $('examples').hidden = hasTurns;
  $('workflow-shortcuts').hidden = hasTurns;
  $('conversation-title').textContent = conversation.title;
  $('conversation-caption').textContent = hasTurns ? `${conversation.turns.length} ${conversation.turns.length === 1 ? 'exchange' : 'exchanges'} · Context retained` : 'New conversation';
  $('conversation-cost').textContent = conversation.estimated_cost_usd == null ? '' : `Est. total ${money(conversation.estimated_cost_usd)}`;
  const blocks = [];
  let evidence = [];
  for (const run of conversation.turns) {
    const turnEvidence = evidence.concat(run.events);
    if (run.status === 'completed') evidence = turnEvidence;
    const section = node('section', undefined, 'chat-turn');
    const user = node('div', undefined, 'user-message');
    user.append(node('span', 'You', 'message-role'), node('p', run.user_message));
    const assistant = node('article', undefined, 'assistant-message');
    const heading = node('div', undefined, 'message-heading');
    heading.append(node('span', '◈ Workspace', 'message-role'));
    if (run.estimated_cost_usd != null) heading.append(node('span', `Est. ${money(run.estimated_cost_usd)}`, 'message-cost'));
    assistant.append(heading);
    const answer = node('div', undefined, 'answer-body');
    const disclosure = node('details', undefined, 'source-disclosure');
    const summary = node('summary');
    const sources = node('div', undefined, 'source-list');
    disclosure.append(summary, sources);
    const citedIds = new Set(Array.from(run.answer.matchAll(/\[([a-zA-Z][\w-]*:[^\]\s]+)\]/g), match => match[1]));
    const visibleRun = {...run, sources: run.sources.filter(source => citedIds.has(source.id)), evidence_events: turnEvidence};
    const options = {prefix: `turn-${run.run_id}-`, disclosure};
    renderAnswer(run.answer || 'This message could not be completed. The previous conversation context is still available. You can retry the question.', visibleRun.sources, answer, options);
    if (run.status === 'failed') answer.classList.add('failed');
    renderSources(visibleRun, {...options, summary, target: sources});
    assistant.append(answer, disclosure);
    const details = node('button', 'View technical details', 'quiet-button turn-details');
    details.addEventListener('click', () => {
      showDiagnostics(run);
      document.querySelector('.developer-disclosure').open = true;
      document.querySelector('.developer-disclosure').scrollIntoView({behavior: 'smooth'});
    });
    assistant.append(details);
    section.append(user, assistant);
    blocks.push(section);
  }
  $('chat-messages').replaceChildren(...blocks);
  $('message').placeholder = hasTurns ? 'Ask a follow-up…' : 'What would you like to know?';
  $('run-button').textContent = 'Send message ↗';
}

async function openConversation(id) {
  if (chatBusy) { notice('Please wait for the current answer before switching conversations.'); return; }
  const conversation = await api(`/api/conversations/${id}`);
  renderConversation(conversation);
  showView('workspace');
  notice('');
  $('message').value = '';
  $('message').scrollIntoView({behavior: 'smooth', block: 'center'});
  $('message').focus({preventScroll: true});
}

async function conversationHistory() {
  if (!conversationSupported) return;
  const conversations = await api('/api/conversations');
  $('conversation-history').replaceChildren(...conversations.map(conversation => {
    const button = node('button', undefined, 'history-item');
    const label = node('span', undefined, 'history-question');
    label.append(node('strong', conversation.title), node('small', `${conversation.revision} ${conversation.revision === 1 ? 'exchange' : 'exchanges'} · ${new Date(conversation.updated_at).toLocaleString()}`));
    button.append(label, node('span', 'Continue →', 'history-action'));
    button.addEventListener('click', () => openConversation(conversation.conversation_id).catch(() => notice('This conversation could not be opened. Start a new one if workspace settings changed.', true)));
    return button;
  }));
  if (!conversations.length) $('conversation-history').append(node('p', 'Your conversations will appear here.', 'empty-state'));
}

$('new-conversation').addEventListener('click', newConversation);
$('task-form').addEventListener('submit', async event => {
  event.preventDefault();
  const message = $('message').value.trim();
  if (!message || chatBusy) return;
  if (!conversationSupported) { notice('Restart the workspace server to enable conversations.', true); return; }
  chatBusy = true;
  $('run-button').disabled = true;
  $('new-conversation').disabled = true;
  $('message').disabled = true;
  notice('Preparing your answer…');
  try {
    if (!conversationId) {
      const conversation = await api('/api/conversations', {});
      renderConversation(conversation);
    }
    if (!pendingMessage || pendingMessage.conversation_id !== conversationId || pendingMessage.message !== message) {
      pendingMessage = {conversation_id: conversationId, message, request_id: crypto.randomUUID()};
    }
    $('chat-thread').hidden = false;
    $('workspace-intro').hidden = true;
    $('examples').hidden = true;
    $('workflow-shortcuts').hidden = true;
    const pending = node('section', undefined, 'chat-turn pending-turn');
    const user = node('div', undefined, 'user-message');
    user.append(node('span', 'You', 'message-role'), node('p', message));
    pending.append(user, node('p', 'Looking through your workspace…', 'pending-answer'));
    $('chat-messages').append(pending);
    const response = await api(`/api/conversations/${conversationId}/messages`, {message, request_id: pendingMessage.request_id});
    renderConversation(response.conversation);
    $('chat-messages').lastElementChild?.scrollIntoView({behavior: 'smooth', block: 'start'});
    pendingMessage = null;
    $('message').value = '';
    notice('');
    await conversationHistory();
  } catch (error) {
    if (error.conversation) {
      renderConversation(error.conversation);
      pendingMessage = null; // This attempt has a saved failure; an explicit retry is a new attempt.
    } else {
      document.querySelector('.pending-turn')?.remove();
    }
    notice(error.status === 409 ? error.message : 'The answer could not be completed. Your previous context is safe. You can retry this message.', true);
    $('message').value = message;
  } finally {
    chatBusy = false;
    $('run-button').disabled = false;
    $('new-conversation').disabled = false;
    $('message').disabled = false;
    $('message').focus({preventScroll: true});
  }
});

document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
  $('message').value = button.dataset.prompt;
  $('message').focus();
}));
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
$('refresh').addEventListener('click', () => Promise.all([history(), conversationHistory()]).catch(() => notice('History could not be refreshed.', true)));

const caseLabels = {
  'acme-open': 'Find the right open tickets',
  'globex-open': 'Keep customer records separate',
  'no-tickets': 'Handle accounts with no open issues',
  'unknown-customer': 'Recognize an unknown customer',
  'ambiguous-company': 'Ask when account names are ambiguous',
  'acme-brief': 'Combine account details and support issues',
  'globex-brief': 'Use the correct account owner and renewal',
  'case-insensitive': 'Handle different capitalization',
  'exact-account': 'Resolve an exact company name',
  'fabricated-ticket-request': 'Avoid inventing a requested ticket',
  'synthetic-account-brief': 'Combine account context and open issues',
  'synthetic-unknown-account': 'Recognize an unknown account',
  'synthetic-ambiguous-name': 'Clarify similar company names',
  'synthetic-primary-contact': 'Find the designated primary contact',
  'synthetic-open-pipeline': 'Calculate open pipeline value',
  'synthetic-no-contacts': 'Handle an account with no contacts',
  'synthetic-missing-email': 'Report missing email information',
  'synthetic-no-primary-designation': 'Avoid guessing a primary contact',
  'synthetic-missing-support-mapping': 'Explain unavailable support data',
  'synthetic-closed-deals-excluded': 'Exclude closed deals from pipeline',
  'synthetic-inclusive-renewal-window': 'Apply an inclusive renewal window',
  'synthetic-critical-renewal-review': 'Review renewal risk with evidence',
  'heldout-primary-contact': 'Find a contact in a different account',
  'heldout-open-pipeline': 'Calculate another account’s pipeline',
  'heldout-closed-tickets-excluded': 'Exclude closed support tickets',
  'heldout-open-status-only': 'Distinguish open, pending and closed',
  'heldout-exact-account-across-systems': 'Resolve an account across CRM and support',
  'heldout-compare-pipelines': 'Compare pipeline across two accounts',
};

function showEvaluation(report) {
  const {passed, total, planned, estimated_cost_usd: cost} = report.summary;
  const score = node('div', undefined, 'quality-score');
  score.append(node('strong', `${passed} of ${total}`), node('span', 'checks passed'));
  const splitLabel = report.split === 'heldout' ? ' · Held-out cases' : '';
  const context = (report.kind === 'live_model' ? 'Live-model results' : 'Scripted demo results · not model accuracy') + splitLabel;
  const caption = report.replayed ? 'Recorded run cost' : 'Estimated cost';
  $('eval-results').replaceChildren(score, node('p', `${context} · ${caption}: ${money(cost)} · ${(report.summary.mean_duration_ms / 1000).toFixed(1)} seconds per question`, 'quality-note'));
  if (report.summary.complete === false) $('eval-results').append(node('p', `${planned - total} scenarios were not run. ${report.stopped_reason || ''}`, 'failed'));
  if (report.baseline_id === report.evaluation_id) $('eval-results').append(node('p', 'Baseline saved. Future checks with the same data and grading rules can be compared with this result.', 'baseline-note'));
  if (report.comparison?.comparable) {
    const delta = Math.round(report.comparison.pass_rate_delta * 100);
    $('eval-results').append(node('p', delta === 0 ? 'Success rate is unchanged from the baseline.' : `Success rate changed by ${delta > 0 ? '+' : ''}${delta} percentage points from the baseline.`, 'quality-note'));
  } else if (report.comparison?.reason) $('eval-results').append(node('p', report.comparison.reason, 'quality-note'));
  for (const result of report.results) {
    const details = node('details', undefined, 'evaluation-case');
    const summary = node('summary', undefined, 'quality-row');
    summary.append(node('span', caseLabels[result.case_id] || 'Workflow check'), node('span', result.passed ? 'Passed' : 'Review needed', result.passed ? 'check-passed' : 'failed'));
    details.append(summary);
    const facts = node('p', `${result.tool_calls} data lookups · ${(result.duration_ms / 1000).toFixed(1)} seconds · Est. ${money(result.estimated_cost_usd)}`, 'quality-note');
    details.append(facts);
    for (const note of result.efficiency_notes || []) details.append(node('p', note, 'efficiency-note'));
    if (!result.passed) details.append(node('p', 'One or more automated expectations did not match. Read the answer alongside its sources before deciding whether this is an agent or grading issue.', 'quality-note'));
    const read = node('button', 'Read answer', 'quiet-button');
    read.addEventListener('click', async () => {
      try {
        showRun(await api(`/api/runs/${result.run_id}`));
        showView('workspace');
        $('result').scrollIntoView({behavior: 'smooth', block: 'start'});
      } catch { notice('The saved answer could not be loaded.', true); }
    });
    details.append(read);
    $('eval-results').append(details);
  }
  $('eval-results').append(node('p', 'Human review is pending. These checks cover selected facts and citations; they do not prove every claim. Cost estimates exclude cache discounts. Reopening a saved report makes no model requests.', 'quality-note'));
}

async function evaluationHistory() {
  if (!evaluationReady) return;
  const reports = await api('/api/evaluations');
  $('evaluation-history').replaceChildren(...reports.slice(0, 10).map(report => {
    const button = node('button', undefined, 'history-item');
    const coverage = report.profile === 'quick' ? 'Quick' : 'Full';
    const title = `${report.kind === 'live_model' ? 'Live model' : 'Demo'} · ${coverage}${report.split === 'heldout' ? ' · Held-out' : ''}${report.replayed ? ' · replay' : ''} · ${report.summary.passed} of ${report.summary.total} passed`;
    const label = node('span', undefined, 'history-question');
    const costLabel = report.kind === 'deterministic_demo' ? 'No API usage' : `${report.replayed ? 'Recorded cost' : 'Est. cost'}: ${money(report.summary.estimated_cost_usd)}`;
    label.append(node('strong', title), node('small', `${new Date(report.created_at).toLocaleString()} · ${costLabel}`));
    button.append(label, node('span', 'View report →', 'history-action'));
    button.addEventListener('click', () => showEvaluation(report));
    return button;
  }));
  if (!reports.length) $('evaluation-history').append(node('p', 'Completed checks will appear here.', 'empty-state'));
}

function updateEvaluationOptions() {
  const counts = evaluationCounts?.[$('evaluation-split').value];
  if (counts) {
    for (const option of $('evaluation-profile').options) {
      option.textContent = `${option.value === 'quick' ? 'Quick' : 'Full'} · ${counts[option.value]} scenarios`;
    }
  }
  $('evaluate').textContent = $('evaluation-profile').value === 'quick' ? 'Run quick check' : 'Run full check';
}
$('evaluation-profile').addEventListener('change', updateEvaluationOptions);
$('evaluation-split').addEventListener('change', updateEvaluationOptions);
$('refresh-evaluations').addEventListener('click', () => evaluationHistory().catch(() => notice('Reports could not be loaded.', true)));
$('evaluate').addEventListener('click', async () => {
  if (!evaluationReady) return;
  $('evaluate').disabled = true;
  $('eval-results').replaceChildren(node('p', 'Checking the selected workflows…', 'empty-state'));
  try {
    showEvaluation(await api('/api/evaluate', {profile: $('evaluation-profile').value, split: $('evaluation-split').value}));
    await Promise.all([history(), evaluationHistory()]);
  } catch { $('eval-results').replaceChildren(node('p', 'The checks could not be completed. Check the workspace connection and model pricing settings before trying again.', 'failed')); }
  finally { $('evaluate').disabled = false; }
});

(async () => {
  try {
    const status = await api('/api/status');
    conversationSupported = status.conversations === true;
    conversationStorageKey = `agent-runtime:conversation:${location.origin}:${status.dataset_hash}:${status.config_hash}:${status.provider}:${status.model}`;
    if (!conversationSupported) {
      $('run-button').disabled = true;
      notice('Restart the workspace server to enable conversations.', true);
    }
    for (const workflow of status.workflow_prompts || []) {
      const button = node('button', workflow.label);
      button.addEventListener('click', () => { $('message').value = workflow.message; $('message').focus(); });
      $('workflow-shortcuts').append(button);
    }
    $('mode').textContent = status.provider === 'demo' ? 'Demo workspace' : 'Connected workspace';
    $('mode-help').textContent = status.provider === 'demo' ?
      'Sample data · Scripted demo · No API usage' : 'Sample data · OpenAI connected · API usage billed';
    evaluationReady = Array.isArray(status.evaluation_profiles);
    evaluationCounts = status.evaluation_case_counts;
    if (status.evaluation_splits?.includes('heldout')) {
      $('evaluation-split').hidden = false;
      $('split-label').hidden = false;
    }
    updateEvaluationOptions();
    if (status.workspace_name) $('workspace-label').textContent = status.workspace_name;
    if (status.dataset_counts && status.as_of) {
      const counts = Object.entries(status.dataset_counts).filter(([key]) => key !== 'support_organizations');
      $('dataset-summary').textContent = `${counts.map(([key, value]) => `${value} ${key}`).join(' · ')} · Snapshot: ${readableDate(status.as_of)}`;
      $('dataset-summary').hidden = false;
    }
    if (status.suggested_questions) {
      if ($('message').value === 'Prepare an account brief for Acme' && status.suggested_questions[0]) {
        $('message').value = status.suggested_questions[0];
      }
      document.querySelectorAll('[data-prompt]').forEach((button, index) => {
        const question = status.suggested_questions[index];
        if (question) { button.dataset.prompt = question; button.textContent = question.replace(/[?]$/, ''); }
      });
    }
    $('evaluate').disabled = !evaluationReady || status.evaluations_ready === false;
    if (!evaluationReady) {
      $('quality-description').textContent = 'Restart the workspace server to enable the new quick checks and saved reports.';
      $('evaluation-history').replaceChildren(node('p', 'Saved reports will be available after the server restarts.', 'empty-state'));
    } else if (status.evaluations_ready === false) {
      $('quality-description').textContent = status.evaluation_block_reason || 'Add gold evaluation cases for your imported data in the workspace configuration before running checks.';
    }
    if (status.connections) $('connections').textContent = status.connections.join(' & ');
    if (status.data_origin === 'synthetic') {
      $('mode-help').textContent = status.provider === 'demo' ?
        'Fictional synthetic data · Scripted demo · No API usage' : 'Fictional synthetic data · OpenAI connected · API usage billed';
    } else if (status.sample_data === false) {
      $('mode-help').textContent = status.provider === 'demo' ? 'Imported CRM data · Scripted demo' : 'Imported CRM data · OpenAI connected · API usage billed';
    }
    $('tools').replaceChildren(...status.tools.map(tool => {
      const details = node('details');
      details.append(node('summary', tool.name), node('p', tool.description), node('pre', JSON.stringify(tool.inputSchema, null, 2)));
      return details;
    }));
    await history();
    await evaluationHistory();
    await conversationHistory();
    let savedId = null;
    try { savedId = localStorage.getItem(conversationStorageKey); } catch { /* No persistent selection. */ }
    if (savedId && conversationSupported) {
      try { await openConversation(savedId); }
      catch { rememberConversation(null); }
    }
  } catch { notice('The workspace is unavailable. Check that the local server is running.', true); }
})();
