"""Amazon order history module.

Amazon exports are already-structured CSV data. Unlike CC/Tax,
the import step is deterministic (unzip → identify profile →
write manifest sidecar) with no AI extraction needed.
"""
