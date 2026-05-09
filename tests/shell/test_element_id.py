from iris.shell.element_id import el, tab_button_id, tab_panel_id


def test_el_combines_tab_id_and_parts():
    assert el("AB12CD34", "results") == "t-AB12CD34-results"


def test_el_handles_multiple_parts():
    assert el("AB12CD34", "row", "5", "edit") == "t-AB12CD34-row-5-edit"


def test_el_with_no_parts_returns_prefix_only():
    assert el("AB12CD34") == "t-AB12CD34-"


def test_tab_button_id_format():
    assert tab_button_id("AB12CD34") == "tab-button-AB12CD34"


def test_tab_panel_id_format():
    assert tab_panel_id("AB12CD34") == "tab-content-AB12CD34"
