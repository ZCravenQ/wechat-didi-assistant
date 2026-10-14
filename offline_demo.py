"""本地模拟完整流程，不访问微信、LLM 或滴滴。"""
from agent import Agent, Store
from test_agent import FakeDidi, ScriptLLM, scenario

didi = FakeDidi()
store = Store(':memory:')
agent = Agent(didi, ScriptLLM(scenario()), store)
print('=== 完全离线演示：模拟模型和模拟滴滴工具 ===')
print(agent.respond('demo', '北京，从起点到终点，请选择测试快车叫车'))
print('\n模拟用户明确确认：确认')
print(agent.respond('demo', '确认'))
print('\n再次回复确认：')
print(agent.respond('demo', '确认'))
print('\n模拟下单工具执行次数：' + str(sum(name == 'taxi_create_order' for name, _ in didi.calls)))
