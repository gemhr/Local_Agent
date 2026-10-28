"""只装载已批准的四份公开快照；调用者不能提供路径或资料正文。"""
import json
from pathlib import Path
from core.feature_impact.knowledge import digest, read

APPROVED_INPUTS = {
    'T2': 'be188c4cf395286ad272a0556485b513279e387c6098499133646fb7e185140a',
    'T3': '60c39e7217e6d32a8dc725f052a9e6b327517eae1bed0a124a05fb59e7e2651a',
    'T5': 'e6ac66135bba5be333a9ad0febea8b0382c762897ffaed6654aa3f6a77c59dc7',
    'T6': '59bd0a56bd9110b58f8d9b9aecf42fe9f9a4ddfaa2a3c367e9ad2103dccfd120',
}


def load_public_task(code):
    if code not in APPROVED_INPUTS:
        raise ValueError('PUBLIC_TASK_NOT_APPROVED')
    value = json.loads((Path(__file__).with_suffix('') / f'{code}.json').read_text(encoding='utf-8'))
    task = value['snapshot']
    if (task['input_digest'] != APPROVED_INPUTS[code]
            or digest({k:v for k,v in task.items() if k != 'input_digest'}) != task['input_digest']
            or digest(task['manifest']) != task['corpus_digest']):
        raise ValueError('PUBLIC_TASK_DIGEST_INVALID')
    for doc in task['documents']:
        read(task, doc['source_ref'])
    return task
