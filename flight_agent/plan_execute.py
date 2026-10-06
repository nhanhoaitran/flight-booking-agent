from langgraph.graph import END, START, StateGraph

from flight_agent.agents import AgentState, FlightAgent


class PlanExecuteAgent(FlightAgent):
    def _build_graph(self):
        graph = StateGraph(AgentState)
        nodes = ('bootstrap', 'plan', 'next_step', 'execute')
        for name in (*nodes, 'finalize'):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, 'bootstrap')
        for name in nodes:
            graph.add_conditional_edges(name, lambda state: state['route'],
                                         {target: target for target in ('plan', 'next_step', 'execute', 'finalize')})
        graph.add_edge('finalize', END)
        return graph.compile()
