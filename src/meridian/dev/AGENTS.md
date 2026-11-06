# Developer tooling

A lint may use `PythonSource.mentions` to skip parsing only for identifier tokens.
String values and dotted names can be spelled through escapes, concatenation or
line continuations, so parse every module for those checks.
