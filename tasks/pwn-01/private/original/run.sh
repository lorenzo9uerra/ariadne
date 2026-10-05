#!/bin/bash
cd deploy && sudo -E env CHALL_HASH='scanfun-f11e365e8cb8174fee4e26a03de55a7c' docker-compose up -d --build scanfun && echo '


If you are on prod or testing server, here is how you connect:' && echo '> ncat --ssl scanfun-f11e365e8cb8174fee4e26a03de55a7c.b01le.rs 8443'