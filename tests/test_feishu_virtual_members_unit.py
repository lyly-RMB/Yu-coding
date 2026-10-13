from project_lens.integrations.feishu.virtual_members import VirtualMemberController


def test_virtual_members_require_enabled_allowlisted_operator_and_isolate_chat() -> None:
    controller = VirtualMemberController(enabled=True, operators="real-user")
    action = controller.command("/pl-sim as product")
    assert action == ("as", "product")
    assert "没有权限" in controller.handle(operator_id="other", chat_id="chat-a", action=action)
    assert controller.active_actor(operator_id="other", chat_id="chat-a") is None
    assert "已切换" in controller.handle(operator_id="real-user", chat_id="chat-a", action=action)
    assert controller.active_actor(operator_id="real-user", chat_id="chat-a") == "virtual-product"
    assert controller.active_actor(operator_id="real-user", chat_id="chat-b") is None


def test_virtual_members_off_restores_real_identity_and_disabled_is_inert() -> None:
    controller = VirtualMemberController(enabled=True, operators="real-user")
    switch = controller.command("/pl-sim as qa")
    clear = controller.command("/pl-sim off")
    assert switch is not None and clear is not None
    controller.handle(operator_id="real-user", chat_id="chat-a", action=switch)
    controller.handle(operator_id="real-user", chat_id="chat-a", action=clear)
    assert controller.active_actor(operator_id="real-user", chat_id="chat-a") is None

    disabled = VirtualMemberController(enabled=False, operators="real-user")
    assert "未启用" in disabled.handle(operator_id="real-user", chat_id="chat-a", action=switch)
    assert disabled.active_actor(operator_id="real-user", chat_id="chat-a") is None
