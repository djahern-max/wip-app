"""Cost from the ledger (F10; BLUEPRINT §7 "Cost", §8.5 check 2; D-51). ``ledger_line``
holds every QuickBooks line that posts to an expense-side account, with the job its
``CustomerRef`` aliases to; every figure is computed on read from the lines, the
confirmed ``account_map`` and the tenant's ``wip_basis`` (``figures.py``), never stored."""
