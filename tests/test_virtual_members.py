from project_lens.integrations.feishu.virtual_members import VirtualMemberController


def test_virtual_member_switch_and_message() -> None:
    controller = VirtualMemberController(enabled=True, operators="real-user")
    command = controller.parse("/sim product hello there team")
    assert command == ("product", "hello there team")
    assert controller.handle(physical_id="real-user", chat_id="chat", command=command) == "virtual-product"
    assert controller.effective_actor(physical_id="real-user", chat_id="chat") == "virtual-product"


def test_virtual_member_rejects_non_operator() -> None:
    controller = VirtualMemberController(enabled=True, operators="real-user")
    command = controller.parse("/sim qa test")
    assert command is not None
    assert "白名单" in controller.handle(physical_id="other", chat_id="chat", command=command)


def test_virtual_member_clear() -> None:
    controller = VirtualMemberController(enabled=True, operators="real-user")
    command = controller.parse("/sim qa test")
    assert command is not None
    controller.handle(physical_id="real-user", chat_id="chat", command=command)
    clear = controller.parse("/sim off")
    assert clear is not None
    controller.handle(physical_id="real-user", chat_id="chat", command=clear)
    assert controller.effective_actor(physical_id="real-user", chat_id="chat") is None
