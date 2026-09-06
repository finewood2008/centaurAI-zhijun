<script setup lang="ts">
import MainLayout from '@/layouts/MainLayout.vue'
import ToastHost from '@/components/ui/ToastHost.vue'
import RoutingConsent from '@/components/conversation/RoutingConsent.vue'
</script>

<template>
  <ToastHost>
    <MainLayout>
      <template v-if="$slots.topbar" #topbar="controls"><slot name="topbar" v-bind="controls" /></template>
    </MainLayout>
    <RoutingConsent />
  </ToastHost>
</template>
