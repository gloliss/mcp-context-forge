"""Regression tests for the Admin UI icon and motion system."""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(relative_path: str) -> str:
    """Read one repository file as UTF-8 text."""
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_sidebar_uses_one_vector_icon_per_link() -> None:
    """Keep platform-dependent emoji out of the primary navigation."""
    template = _read("mcpgateway/templates/admin.html")
    sidebar = template.split("<!-- Sidebar Navigation -->", 1)[1].split("<!-- Sidebar Footer -->", 1)[0]

    link_count = len(re.findall(r'class="sidebar-link\b', sidebar))
    assert link_count > 20
    assert sidebar.count('class="sidebar-icon"') == link_count
    assert len(re.findall(r'aria-hidden="true"><i class="fa-', sidebar)) == link_count
    assert not re.search(r"[📊🖥️🔗🛠️⚙️💬📁🌳📦🤖🔌🌐🗄️🧪👨‍💻⚡🔍📈🧩🗃️👥👤🎫📤📋ℹ️🔧]", sidebar)


def test_runtime_labels_use_python_and_rust_brand_icons() -> None:
    """Render stable vector brand marks instead of snake/crab emoji."""
    for relative_path in (
        "mcpgateway/templates/overview_partial.html",
        "mcpgateway/templates/version_info_partial.html",
    ):
        template = _read(relative_path)
        assert "fa-brands fa-python" in template
        assert "fa-brands fa-rust" in template
        assert "🐍" not in template
        assert "🦀" not in template


def test_motion_has_reduced_motion_fallback() -> None:
    """Do not force decorative movement on reduced-motion users."""
    stylesheet = _read("mcpgateway/static/admin.css")
    assert "@keyframes cf-panel-enter" in stylesheet
    assert "@keyframes cf-icon-pop" in stylesheet
    assert "@keyframes cf-runtime-breathe" in stylesheet
    reduced_motion = stylesheet.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".runtime-brand-mark--hero" in reduced_motion
    assert "animation: none" in reduced_motion
