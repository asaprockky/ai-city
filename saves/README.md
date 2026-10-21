# Saved worlds

`asapville.db` is a snapshot of the running town (citizens, memories, businesses, bank).
To continue from it on a fresh checkout:

    cp saves/asapville.db asapville.db
    ASAPVILLE_LLM_BACKEND=anthropic asapville run --ticks 12 --fast
