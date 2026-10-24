"""App skills: markdown + frontmatter documents describing how to drive one app.

Bundled skills live in ``skills/builtin/*.md``; user and generated skills live in
the user folder (``WINDOWS_MCP_SKILLS_DIR`` or the platformdirs data dir).
"""

from windows_mcp.skills.models import Skill
from windows_mcp.skills.store import SkillStore, default_user_dir, get_store

__all__ = ["Skill", "SkillStore", "default_user_dir", "get_store"]
