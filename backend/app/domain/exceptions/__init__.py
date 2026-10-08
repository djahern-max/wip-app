"""The exceptions queue (F09; BLUEPRINT §7 "Work queue", §10, §11; D-13, D-22, D-46).

A review sentence a generator raises is held as an **exception** with an identity that
lasts across runs: open until its cause is gone (resolved by the run) or a person
dismisses it with a note. Nothing here computes money or changes a generator: the
generators in ``app.domain.estimates.exceptions``, ``app.domain.jobs.issues`` and
``app.domain.billing.pay_applications`` are called as they are and what they return is
persisted (``run.py``). ``collect.py`` is the one place a subject's sentences are
assembled, for the pages and the run alike; ``registry.py`` gives every code its
severity and identity key; ``service.py`` holds the queue's reads and the person's
actions; ``models.py`` the two tables of migration 0016.
"""
