"""Business logic for settlement, independent of HTTP and Celery.

Views and tasks are thin adapters around these modules, which keeps the rules
testable with plain function calls.
"""
