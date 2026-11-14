# Conventions

- Errors: raise a subclass of `AppError` from `app/errors.py`, never a bare
  `Exception`. The API layer maps each `AppError` subclass to a status code;
  anything else becomes a 500 with no message for the caller.
- Type hints on every function signature.
