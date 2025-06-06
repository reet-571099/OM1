import asyncio
import logging

from runtime.evaluation_logger import AgentEvaluationLogger

from actions.orchestrator import ActionOrchestrator
from fuser import Fuser
from inputs.orchestrator import InputOrchestrator
from providers.io_provider import IOProvider
from providers.sleep_ticker_provider import SleepTickerProvider
from runtime.config import RuntimeConfig
from simulators.orchestrator import SimulatorOrchestrator


class CortexRuntime:
    """
    The main entry point for the OM1 agent runtime environment.

    The CortexRuntime orchestrates communication between memory, fuser,
    actions, and manages inputs/outputs. It controls the agent's execution
    cycle and coordinates all major subsystems.

    Parameters
    ----------
    config : RuntimeConfig
        Configuration object containing all runtime settings including
        agent inputs, cortex LLM settings, and execution parameters.
    """

    config: RuntimeConfig
    fuser: Fuser
    action_orchestrator: ActionOrchestrator
    simulator_orchestrator: SimulatorOrchestrator
    sleep_ticker_provider: SleepTickerProvider

    def __init__(self, config: RuntimeConfig, debug_once=False):
        """
        Initialize the CortexRuntime with provided configuration.

        Parameters
        ----------
        config : RuntimeConfig
            Configuration object for the runtime.
        """
        self.config = config
        self.fuser = Fuser(config)
        self.action_orchestrator = ActionOrchestrator(config)
        self.simulator_orchestrator = SimulatorOrchestrator(config)
        self.sleep_ticker_provider = SleepTickerProvider()
        self.io_provider = IOProvider()
        self.speech_duty_cycle = 0

        # For Dev purposes only - Limit the number of ticks to 1
        # and runs the cortex once
        self.debug_once = debug_once

    
        self.eval_logger = AgentEvaluationLogger() 


    async def run(self) -> None:
        """
        Start the runtime's main execution loop.

        This method initializes input listeners and begins the cortex
        processing loop, running them concurrently.

        Returns
        -------
        None
        """
        input_listener_task = await self._start_input_listeners()
        cortex_loop_task = asyncio.create_task(self._run_cortex_loop())

        simulator_start = self._start_simulator_task()
        action_start = self._start_action_task()

        await asyncio.gather(
            input_listener_task, cortex_loop_task, simulator_start, action_start
        )

    async def _start_input_listeners(self) -> asyncio.Task:
        """
        Initialize and start input listeners.

        Creates an InputOrchestrator for the configured agent inputs
        and starts listening for input events.

        Returns
        -------
        asyncio.Task
            Task handling input listening operations.
        """
        input_orchestrator = InputOrchestrator(self.config.agent_inputs)
        input_listener_task = asyncio.create_task(input_orchestrator.listen())
        return input_listener_task

    async def _start_simulator_task(self) -> asyncio.Future:
        return self.simulator_orchestrator.start()

    async def _start_action_task(self) -> asyncio.Future:
        return self.action_orchestrator.start()

    async def _run_cortex_loop(self) -> None:
        """
        Execute the main cortex processing loop.

        Runs continuously, managing the sleep/wake cycle and triggering
        tick operations at the configured frequency.

        Returns
        -------
        None
        """

        # Dev purposes only - Limit the number of ticks to 1
        if self.debug_once:
            # Run only once for debugging purposes
            await self._tick()
        else:
            while True:
                if not self.sleep_ticker_provider.skip_sleep:
                    await self.sleep_ticker_provider.sleep(1 / self.config.hertz)
                await self._tick()
                self.sleep_ticker_provider.skip_sleep = False

    async def _tick(self) -> None:
        """
        Execute a single tick of the cortex processing cycle.

        Collects inputs, generates prompts, processes them through the LLM,
        and triggers appropriate simulators and actions based on the output.

        Returns
        -------
        None
        """
        # collect all the latest inputs
        finished_promises, _ = await self.action_orchestrator.flush_promises()

        # combine those inputs into a suitable prompt
        prompt = self.fuser.fuse(self.config.agent_inputs, finished_promises)
        if prompt is None:
            logging.warning("No prompt to fuse")
            return

        # if there is a prompt, send to the AIs
        output = await self.config.cortex_llm.ask(prompt)
        if output is None:
            logging.warning("No output from LLM")
            return

        # Trigger the simulators
        await self.simulator_orchestrator.promise(output.commands)

        commands_silent = []
        for command in output.commands:
            action_type = command.type
            if action_type != "speak":
                commands_silent.append(command)
                logging.debug(f"appended: {action_type}")

        # Trigger actions
        if ("Voice INPUT" in prompt) or ("WalletCoinbase" in prompt):
            # always respond to voice input
            await self.action_orchestrator.promise(output.commands)
        elif self.config.name == "spot_speak":
            # spot, the speaking dog
            await self.action_orchestrator.promise(output.commands)
        elif self.config.name == "turtle_speak":
            # flash, the smart vaccuum cleaner
            # reduce continuous narration
            self.speech_duty_cycle += 1
            if self.speech_duty_cycle > 12:
                # speak
                await self.action_orchestrator.promise(output.commands)
                self.speech_duty_cycle = 0
            else:
                # do not speak
                await self.action_orchestrator.promise(commands_silent)
        else:
            # do not send speech to speaker but only to simulator
            await self.action_orchestrator.promise(commands_silent)
            
        # Custom Logger for Evaluation
        # Log the evaluation tick with prompt, output, and actions
        self.eval_logger.log_tick(
        prompt=prompt,
        output=output,
        actions=[command.dict() for command in output.commands],
        meta={
            "agent": self.config.name,
            "duty_cycle": self.speech_duty_cycle
    }
)
