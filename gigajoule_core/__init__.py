"""Gigajoule core: units, schema, ledger and the decisions underneath them.

Role: package marker
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes

Deliberately empty of logic. A package __init__ that imports its submodules
makes every later import order-dependent and turns a cheap `import
gigajoule_core.units` into a load of the database layer -- which is how a
read-only report ends up opening a connection it never uses.
"""
