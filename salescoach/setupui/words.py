"""The words the setup pages use for a model provider, written for a sales leader rather than for
the person who installed the coach. providers.catalog() stays the source of what exists and what
state it is in; this only names it."""

# Copy for a sales leader: what each provider is, and where its key comes from. Consoles are named,
# never linked: a URL this code cannot verify is not something to send a user to.
PROVIDER_COPY = {
    "claude_code": {"short": "Claude subscription", "name": "Claude, through your Claude subscription", "kind": "No API key",
                    "summary": "Uses the Claude subscription already signed in on this Mac. Nothing to paste.",
                    "key_help": "",
                    "needs": "Needs the Claude CLI installed and signed in on this machine."},
    "anthropic": {"short": "Anthropic API", "name": "Claude, with an Anthropic API key", "kind": "API key",
                  "summary": "Claude, billed to your own Anthropic account.",
                  "key_help": "Create a key in the Anthropic Console, under API keys, and paste it here.",
                  "needs": ""},
    "openai": {"short": "OpenAI", "name": "OpenAI (GPT)", "kind": "API key",
               "summary": "GPT models, billed to your OpenAI account. You pick the two models after loading the list.",
               "key_help": "Create a key on the OpenAI platform dashboard, under API keys, and paste it here.",
               "needs": ""},
    "xai": {"short": "xAI", "name": "xAI (Grok)", "kind": "API key",
            "summary": "Grok models, billed to your xAI account. You pick the two models after loading the list.",
            "key_help": "Create a key in the xAI console, under API keys, and paste it here.",
            "needs": ""},
    "openai_compatible": {"short": "Your own gateway", "name": "Your company's own model gateway", "kind": "Address, key optional",
                          "summary": "For a company that runs its own gateway or model server that speaks the OpenAI format.",
                          "key_help": "Your IT team gives you the address and, if it needs one, the key. "
                                      "Leave the key empty for a server that does not ask for one.",
                          "needs": "The address must start with https unless the server is on this machine or network."},
    "ollama": {"short": "Ollama", "name": "Ollama (models on this Mac)", "kind": "No API key",
               "summary": "Runs on this machine and keeps everything local. Small local models struggle with "
                          "hour-long call transcripts.",
               "key_help": "", "needs": "Needs Ollama running, with a model already downloaded."},
}


def provider_name(key: str, fallback: str = "", short: bool = False) -> str:
    found = PROVIDER_COPY.get(key) or {}
    return found.get("short" if short else "name") or fallback or key
