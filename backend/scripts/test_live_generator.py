import os
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.test_generator_agent import test_generator_agent_node
from app.agents.state import AgentState

state = {
    'pr_id': 'pr-test-gen',
    'repository_id': 'repo-demo',
    'changed_nodes': [{
        'name': 'process_payment',
        'type': 'function',
        'file_path': 'app/services/billing.py',
        'line_start': 1,
        'line_end': 10
    }],
    'change_contracts': [{
        'function_name': 'process_payment',
        'file_path': 'app/services/billing.py',
        'signature': 'def process_payment(amount: float, currency: str = "USD") -> bool',
        'docstring': 'Charge client account with given amount. Raises ValueError if amount <= 0.',
        'parameters': [{'name': 'amount', 'type': 'float'}, {'name': 'currency', 'type': 'str'}],
        'return_type': 'bool',
        'raises': ['ValueError']
    }],
    'test_targets': [{
        'file': 'app/services/billing.py',
        'score': 50,
        'priority': 'high',
        'reasons': ['payment gateway core logic']
    }],
    'context_health': {'mode': 'sufficient', 'score': 90, 'checks': {'contracts': True}},
    'existing_tests': [],
    'retrieved_context': [],
    'test_config': {'framework': 'pytest', 'test_command': 'pytest'}
}

result = test_generator_agent_node(state)
print('GENERATED TESTS COUNT:', len(result.get('generated_tests', [])))
for t in result.get('generated_tests', []):
    print('---')
    print('NAME:', t.get('function_name'))
    print('TYPE:', t.get('test_type'))
    print('FILE:', t.get('test_file_path'))
    print(t.get('content'))
