# TODO

# Refactor APD Press Release poller (`modules/pollers.py` lines 218-630) into `modules/pollers/impl/apd_news.py` - COMPLETE
# [x] Refactor APD Press Release poller (`modules/pollers.py` lines 218-630) into `modules/pollers/impl/apd_news.py`

# Add retry logic with exponential backoff to LLM calls in modules/llm.py
# [x] Add retry mechanism to _call_openrouter_llm function
# [ ] Add retry mechanism to groq_analyze function for transient failures
# [ ] Implement exponential backoff with jitter
# [ ] Add max retry attempts (3-5)
# [ ] Test with mock failures