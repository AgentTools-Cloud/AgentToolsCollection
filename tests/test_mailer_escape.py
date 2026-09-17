#!/usr/bin/env python3
"""Regression coverage for user-controlled values in notification HTML."""
import os
import sys
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
failed=0
for label,ok in checks.items():
    failed+=not ok
    print('  %s %s'%('ok  ' if ok else 'FAIL',label))
print('\n%d passed, %d failed'%(len(checks)-failed,failed))
raise SystemExit(bool(failed))
