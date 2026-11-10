#!/usr/bin/env python3
"""Regression coverage for user-controlled values in notification HTML."""
import os
import sys
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from directory import mailer

name='<img src=x onerror="alert(1)">'
url='https://agent-tools.cloud/services/x?x=" onmouseover="alert(1)'
line='https://evil.example/<script>alert(1)</script> · base'
reason='<a href="https://evil.example">click me</a>'

approval=mailer._approval_html(name,url,line)
rejection=mailer._rejection_html(name,reason)
checks={
    'approval name escaped': name not in approval and '&lt;img' in approval,
    'approval URL attribute escaped': 'onmouseover="alert(1)' not in approval and '&quot; onmouseover=' in approval,
    'approval verified line escaped': '<script>' not in approval and '&lt;script&gt;' in approval,
    'rejection name escaped': name not in rejection and '&lt;img' in rejection,
    'rejection reason escaped': reason not in rejection and '&lt;a href=&quot;' in rejection,
    'fixed template markup remains': '<table role="presentation"' in approval and '<table role="presentation"' in rejection,
}
with patch.object(mailer, '_send', side_effect=AssertionError('preview must not send')) as sender:
    subject, invitation_text, invitation_html = mailer.claim_invitation_message(
        name, '<b>MCP Registry</b>', 'https://agent-tools.cloud/mcp/example',
        'https://agent-tools.cloud/email/claim-invitations/unsubscribe#preview',
    )
checks.update({
    'invitation preview never sends': sender.call_count == 0,
    'invitation name and source escaped': name not in invitation_html and '&lt;img' in invitation_html and '&lt;b&gt;' in invitation_html,
    'invitation is not a submission receipt': 'Thanks for submitting' not in invitation_text,
    'invitation requires host control': 'verifying control of your host' in invitation_text,
    'invitation has free claim and opt-out': 'Claiming is free' in invitation_text and 'Stop these invitations:' in invitation_text,
    'invitation does not subscribe': "won't subscribe you" in invitation_text,
})
unsafe_refused = True
for invalid_url in ('http://agent-tools.cloud/example', 'https://evil.example/example', 'https://user:password@agent-tools.cloud/example', 'https://agent-tools.cloud:444/example'):
    try:
        mailer.claim_invitation_message('Example', 'MCP Registry', invalid_url, 'https://agent-tools.cloud/email/claim-invitations/unsubscribe#preview')
    except ValueError:
        pass
    else:
        unsafe_refused = False
checks['invitation unsafe links refused'] = unsafe_refused
failed=0
for label,ok in checks.items():
    failed+=not ok
    print('  %s %s'%('ok  ' if ok else 'FAIL',label))
print('\n%d passed, %d failed'%(len(checks)-failed,failed))
raise SystemExit(bool(failed))
