# Code runner

A JavaScript code runner. Send code to `POST /run`:

```json
{"code": "out.tag(\"result\").line(\"done\"); \"ok\""}
```

The response is `{"result": ..., "output": ...}`.

## Runtime API

`out` is a fluent output helper:

```js
out.tag("result").line("done");
```

`out` methods return the helper, so calls can be chained. The runtime has no
filesystem, process or network access.
