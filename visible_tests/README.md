# Visible tests

These demonstrate the shape of the API and a little of its behaviour, so you
can check your work as you go.

Run them with a server on the default port:

    redis-server --port 6379 --save '' --appendonly no \
                 --enable-debug-command yes --daemonize yes
    cd /app && python -m pytest tests/ -v

Set `RESP3_TEST_PORT` if your server is somewhere else.

## The seed

Anything random here draws from the fixed seed **20260818**, so a failure you
see once you can see again. It is printed at the start of the run.

## What these are not

They are a starting point, not the standard. They use one published seed, they
split the parser's input at a handful of obvious places rather than
adversarially, they touch a few commands rather than the full matrix, and they
leave the pool's failure handling alone entirely.

Your work is checked against far more than this: fragmentation under seeds you
have not seen, concurrent pool traffic with connections failing underneath it,
memory behaviour under large payloads, and a much wider command surface
compared against a reference client. Passing everything here is necessary and
a long way from sufficient.

Read `instruction.md` for what is actually required.
