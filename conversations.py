"""Local conversation state. Provider history is private; the browser gets turns only."""
import fcntl
import json
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from tracing import utc_now


class ConversationBusy(ValueError):
    pass


class ConversationMismatch(ValueError):
    pass


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('Invalid conversation ID')
    return value


class ConversationStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def create(self, provider, model, dataset_hash, config_hash):
        conversation = {
            'conversation_id': uuid4().hex, 'title': 'New conversation',
            'created_at': utc_now(), 'updated_at': utc_now(), 'provider': provider, 'model': model,
            'dataset_hash': dataset_hash, 'config_hash': config_hash,
            'turns': [], 'model_state': None, 'sources': [], 'revision': 0,
        }
        self.save(conversation)
        return conversation

    def get(self, conversation_id):
        path = self.directory / (valid_id(conversation_id) + '.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def save(self, conversation):
        conversation_id = valid_id(conversation['conversation_id'])
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / (conversation_id + '.json')
        temporary = path.with_name(conversation_id + '-' + uuid4().hex + '.tmp')
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(conversation, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    @contextmanager
    def locked(self, conversation_id):
        valid_id(conversation_id)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(self.directory / (conversation_id + '.lock')), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConversationBusy('An answer is already being prepared in this conversation. Please wait.') from None
            try:
                yield self.get(conversation_id)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def list_conversations(self, provider, model, dataset_hash, config_hash, limit=30):
        if not self.directory.exists():
            return []
        paths = sorted(self.directory.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)
        result = []
        for path in paths:
            conversation = json.loads(path.read_text(encoding='utf-8'))
            identity = (conversation['provider'], conversation['model'], conversation['dataset_hash'], conversation['config_hash'])
            if identity != (provider, model, dataset_hash, config_hash):
                continue
            result.append(self.summary(conversation))
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def summary(conversation):
        return {key: deepcopy(conversation[key]) for key in (
            'conversation_id', 'title', 'created_at', 'updated_at', 'provider', 'model', 'revision'
        )}
