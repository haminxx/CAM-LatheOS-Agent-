"""Administrative tools (not part of the serving FastAPI app).

Everything here runs out-of-band: token provisioning, quota top-ups, DynamoDB
table setup. Kept in a separate package so the serving image never accidentally
exposes an admin endpoint.
"""
