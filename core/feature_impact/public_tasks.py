"""只装载已批准的公开快照；调用者不能提供路径或资料正文。"""
import json
from pathlib import Path
from core.feature_impact.knowledge import digest, read

APPROVED_INPUTS = {
    'interface-range-analysis': 'a099a3ce13e0fbdd1f5d5ca4083c033971005bc2718639733189e5efdcfd6a9d',
    'interface-range-analysis-dev': '7c08a58846cba9a8782ed44397d83e4a1690fa7309ff608e8475314895e5ef0a',
    'interface-message-analysis': '85bb61b0bfc1cf3ca8d264236ecf68b19cc5347d4ba62acc11c4a6f74d44de10',
    'interface-message-analysis-dev': '564902136c5030ae4f34ee986c0c196558b1cf543c5ba838cfd5907ea88c55a5',
    'interface-recovery-analysis': 'eaf0a4d10175adb04ca626ee48cfbad784cecd6c4ddda851f240b8b5aab5f2b3',
    'interface-recovery-analysis-dev': '1869b4ff26adba964f253c9c089aff0b887edf0792cad09403f359b7fe0a4e99',

    'sampling-interface': '57faace5956f727f25918aff04d2fa3e1a980b5b5da7dc9bdffc143fe6da3819',
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
