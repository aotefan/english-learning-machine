"""Read this ECS instance's real bill via existing OAuth CLI; export minimal data.
No account identity, API keys, orders or other instances enter the dashboard.
"""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess


def summarize(rows,instance,cycle):
    selected=[]
    for row in rows:
        if row.get('InstanceID') != instance:
            raise ValueError('Bill response includes an unexpected instance')
        if row.get('Currency') != 'CNY':raise ValueError('Unexpected bill currency')
        amount=Decimal(str(row['PretaxAmount']))
        if not amount.is_finite():raise ValueError('Invalid bill amount')
        selected.append({'product':row.get('ProductDetail'),'subscription':row.get('SubscriptionType'),
                         'service_period':row.get('ServicePeriod'),'period_unit':row.get('ServicePeriodUnit'),
                         'amount_due_cny':float(amount)})
    return {'source':'Alibaba Cloud DescribeInstanceBill','billing_cycle':cycle,
            'instance_id':instance,'checked_at':datetime.now(timezone.utc).isoformat(),
            'amount_due_cny':float(sum(Decimal(str(r['amount_due_cny'])) for r in selected)) if selected else None,
            'currency':'CNY','payment_status':'not_verified','items':selected,
            'note':'Current-month bill is provisional and may be delayed; prepaid yearly charges are not monthly recurring fees.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--billing-cycle',required=True)
    p.add_argument('--instance',required=True)
    p.add_argument('--profile',default='english-edge')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--direct',action='store_true',help='Omit proxy environment for this read-only CLI subprocess')
    a=p.parse_args()
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',a.billing_cycle):raise ValueError('Invalid billing cycle')
    env=dict(os.environ)
    if a.direct:env={k:v for k,v in env.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy')}
    rows=[];token=None
    for _ in range(20):
        args=['aliyun','bssopenapi','DescribeInstanceBill','--BillingCycle',a.billing_cycle,
              '--InstanceID',a.instance,'--Granularity','MONTHLY','--MaxResults','100',
              '--region','cn-hangzhou','--profile',a.profile]
        if token:args+=['--NextToken',token]
        result=subprocess.run(args,capture_output=True,text=True,env=env,timeout=30)
        if result.returncode:raise RuntimeError('Billing CLI failed; no snapshot overwritten')
        data=json.loads(result.stdout)
        if not data.get('Success'):raise RuntimeError('Billing API rejected query')
        data=data['Data'];items=data.get('Items',[])
        rows.extend(items.get('Item',[]) if isinstance(items,dict) else items)
        token=data.get('NextToken')
        if not token:break
    else:raise RuntimeError('Billing pagination exceeded limit')
    summary=summarize(rows,a.instance,a.billing_cycle)
    os.umask(0o077);a.output.parent.mkdir(parents=True,exist_ok=True)
    temp=a.output.with_suffix('.new');temp.write_text(json.dumps(summary,ensure_ascii=False,indent=2));temp.chmod(0o600);temp.replace(a.output)
    print(json.dumps({'billing_cycle':a.billing_cycle,'items':len(rows),'amount_due_cny':summary['amount_due_cny']}))


if __name__=='__main__':main()
