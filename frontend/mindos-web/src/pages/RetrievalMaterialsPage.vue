<template>
  <section class="page retrieval-materials">
    <h1>在对话中使用资料</h1>
    <p>资料的上传、整理和索引由 Data Engine 管理，知君负责检索和对话。</p>
    <ol>
      <li>在对话中说明要查找的资料名称、主题或问题。</li>
      <li>查看检索片段，选择本轮要使用的材料及可用的脱敏或原文方式。</li>
      <li>使用在线模型时，再核对本轮允许发送的内容。</li>
    </ol>
    <p>未选中的片段不会用于本轮回答。找不到时可以补充关键词，不会自动读取原件。</p>
    <RouterLink to="/chat" class="retrieval-materials__link">去对话检索资料</RouterLink>
    <p class="retrieval-materials__note">原有资料不会因入口调整被删除；资料维护请使用 Data Engine 的管理入口。</p>
  </section>
</template>

<style scoped>
.retrieval-materials { max-width: 52rem; line-height: 1.8; }
.retrieval-materials h1 { font-size: 1.5rem; }
.retrieval-materials li { margin-block: .5rem; }
.retrieval-materials__link { display: inline-block; margin-block: 1rem; color: var(--color-primary, #a6452e); }
.retrieval-materials__note { color: var(--color-text-secondary, #72736d); font-size: .875rem; }
</style>
