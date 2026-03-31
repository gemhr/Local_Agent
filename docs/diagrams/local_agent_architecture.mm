<map version="1.0.1">
  <node TEXT="Local Agent 系统架构与业务流程">
    <node TEXT="交互层">
      <node TEXT="Desktop UI / Chat Panel"/>
      <node TEXT="FastAPI 接口">
        <node TEXT="/api/chat"/>
        <node TEXT="/api/history"/>
        <node TEXT="/api/memory"/>
        <node TEXT="/api/search"/>
      </node>
    </node>
    <node TEXT="编排层 AgentRouter">
      <node TEXT="Core Router">
        <node TEXT="直接回答"/>
        <node TEXT="委派专家Agent"/>
      </node>
      <node TEXT="Specialists">
        <node TEXT="Data Analyst"/>
        <node TEXT="Code Expert"/>
        <node TEXT="Knowledge Expert"/>
      </node>
      <node TEXT="策略模块">
        <node TEXT="工具规划 Tool Planner"/>
        <node TEXT="RAG Query Rewrite + Rerank"/>
        <node TEXT="记忆蒸馏 Summary Rollup"/>
      </node>
    </node>
    <node TEXT="能力层">
      <node TEXT="Local LLM Engine"/>
      <node TEXT="Tool Registry">
        <node TEXT="list_files"/>
        <node TEXT="analyze_excel"/>
        <node TEXT="get_system_status"/>
      </node>
      <node TEXT="MemoryManager(SQLite)">
        <node TEXT="messages"/>
        <node TEXT="conversation_summaries"/>
        <node TEXT="FTS 检索"/>
      </node>
    </node>
    <node TEXT="知识层">
      <node TEXT="Document Loader">
        <node TEXT="md/txt/pdf/docx/xlsx/csv"/>
        <node TEXT="chunk metadata schema v1"/>
      </node>
      <node TEXT="VectorDBManager">
        <node TEXT="BGE Embedding"/>
        <node TEXT="Chroma 分批 upsert"/>
      </node>
      <node TEXT="Local Knowledge Base">
        <node TEXT="data/knowledge_base"/>
      </node>
    </node>
    <node TEXT="业务闭环">
      <node TEXT="用户提问"/>
      <node TEXT="路由/工具/检索"/>
      <node TEXT="答案生成 + 引用来源"/>
      <node TEXT="记忆落库 + 自动蒸馏"/>
    </node>
  </node>
</map>
